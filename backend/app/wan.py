"""WAN provider / carrier-edge routers — an isolated, read-only visibility module.

These are the WAN providers' routers (e.g. Centrilogic-managed circuits), NOT Medline
devices. This module collects them read-only from LogicMonitor and serves a dedicated
page (list + executive summary + device detail with routing: BGP / OSPF / EIGRP /
IP-routing stats / interfaces / neighbours). It is deliberately self-contained:

  * it reads/writes ONLY the `wan_routers` table — never Device / InventoryDevice;
  * nothing here feeds the Fleet SLA, Overview, resilience, trends or reports;
  * only administrators may add, remove, or refresh a router;
  * missing telemetry is reported as "Not monitored" — never fabricated.
"""
import asyncio
import logging
import re
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import administrator, current_user
from .collection import OSPF_STATE, latest, numeric, state
from .config import get_settings
from .db import AuditEvent, WanRouter, SessionLocal, session
from .logicmonitor import LogicMonitorClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/wan", tags=["wan"])
_refresh_lock = asyncio.Lock()

# BGP finite-state-machine peer states (RFC 4271 order used by the SNMP MIB).
BGP_STATE = {1: "idle", 2: "connect", 3: "active", 4: "opensent", 5: "openconfirm", 6: "established"}
# Datasources we read per router. Absent/zero-instance ones become "Not monitored".
_HEALTH = ["Ping", "HostStatus", "Cisco_CPU_SNMP", "Cisco_MemoryPools_SNMP", "Cisco_TemperatureSensors",
           "SNMP_Host_Uptime", "SNMP_Network_Interfaces", "Device_Component_Inventory",
           "BGP-", "OSPF_Neighbors", "Cisco_EIGRP_Peers", "System Level IP Stats-",
           "LLDP_Neighbors", "CDP_Neighbors"]


def _split_name(display_name: str) -> tuple[str, str]:
    """Best-effort provider/site split from a name like
    'Centrilogic- 900 Harbourside - Telus 100M_VLAN 101' -> (site, provider/circuit)."""
    core = re.sub(r"^\s*Centrilogic-\s*", "", display_name or "").strip()
    parts = [p.strip() for p in core.split(" - ", 1)]
    if len(parts) == 2:
        return parts[0], parts[1]
    return core, ""


async def collect_wan_router(client: LogicMonitorClient, lm_device_id: int, remote: dict) -> dict:
    """Read one WAN router's live state from LogicMonitor (read-only) and normalise it
    into a snapshot dict with routing/interface/health tables plus a routing-coverage
    matrix. Everything not exposed for the device is reported as Not monitored."""
    end = int(time.time())
    props, applied = await asyncio.gather(client.properties(lm_device_id), client.applied_datasources(lm_device_id))
    by_name = {(x.get("dataSourceName") or x.get("name")): x for x in applied}
    sem = asyncio.Semaphore(8)

    async def source(name, hours=1):
        ds = by_name.get(name)
        if not ds:
            return None, []  # datasource not applied to this device at all
        hds = int(ds["id"])
        instances = await client.instances(lm_device_id, hds)

        async def one(instance):
            async with sem:
                try:
                    data = await client.instance_data(lm_device_id, hds, int(instance["id"]), end - hours * 3600, end)
                    return instance, data
                except Exception:
                    return instance, {}
        return ds, await asyncio.gather(*(one(x) for x in instances))

    results = dict(zip(_HEALTH, await asyncio.gather(*(source(n, 24 if n == "Ping" else 1) for n in _HEALTH))))

    # --- reachability / health ---
    ping_pairs = results["Ping"][1]
    ping_data = ping_pairs[0][1] if ping_pairs else {}
    ping_now = latest(ping_data)
    loss = ping_now.get("PingLossPercent")
    pidx = {name: i for i, name in enumerate(ping_data.get("dataPoints", []))}
    losses = [v for r in ping_data.get("values", []) if "PingLossPercent" in pidx and (v := numeric(r[pidx["PingLossPercent"]])) is not None]
    reachability = 100 - (sum(losses) / len(losses)) if losses else None

    cpu_vals = [latest(d) for _, d in results["Cisco_CPU_SNMP"][1]]
    cpu = max((x.get("CPU1min") for x in cpu_vals if isinstance(x.get("CPU1min"), (int, float))), default=None)
    mem_vals = [latest(d) for _, d in results["Cisco_MemoryPools_SNMP"][1]]
    free = min((x.get("PercentFree") for x in mem_vals if isinstance(x.get("PercentFree"), (int, float))), default=None)
    memory = 100 - free if free is not None else None
    temps = [t for _, d in results["Cisco_TemperatureSensors"][1] if isinstance((t := latest(d).get("Temperature") or latest(d).get("sensor_value")), (int, float))]
    temperature = max(temps, default=None)
    uptime = next((latest(d).get("Uptime") for _, d in results["SNMP_Host_Uptime"][1] if isinstance(latest(d).get("Uptime"), (int, float))), None)

    # --- interfaces ---
    interface_rows = []
    for inst, data in results["SNMP_Network_Interfaces"][1]:
        v = latest(data)
        display = re.sub(r"\s*\[ID:\d+\]$", "", inst.get("displayName") or inst.get("name", ""))
        interface_rows.append({"Interface": display, "Description": inst.get("description"), "Status": state(v.get("OperState")),
                               "Admin State": state(v.get("AdminState")), "Speed": v.get("InInterfaceSpeed") or v.get("OutInterfaceSpeed"),
                               "In Utilization %": v.get("InUtilizationPercent"), "Out Utilization %": v.get("OutUtilizationPercent"),
                               "RX Errors": v.get("InErrors"), "TX Errors": v.get("OutErrors"), "Flaps": v.get("StatusFlap")})

    # --- BGP peers ---
    bgp_rows = []
    for inst, data in results["BGP-"][1]:
        v = latest(data)
        st = v.get("PeerState")
        bgp_rows.append({"Peer": inst.get("displayName") or inst.get("wildValue"), "Local Router": inst.get("description"),
                         "State": BGP_STATE.get(int(st), "unknown") if isinstance(st, (int, float)) else "Not available from LogicMonitor",
                         "Admin": "up" if v.get("PeerAdminStatus") == 2 else "down",
                         "Established (s)": v.get("EstablishedTime"), "Resets": v.get("PeerRestart"),
                         "In Updates": v.get("PeerInUpdates"), "Out Updates": v.get("PeerOutUpdates")})

    # --- OSPF neighbours ---
    ospf_rows = []
    for inst, data in results["OSPF_Neighbors"][1]:
        v = latest(data)
        ps = v.get("peerState")
        ospf_rows.append({"Neighbor": inst.get("displayName") or inst.get("name"),
                          "State": OSPF_STATE.get(int(ps), "unknown") if isinstance(ps, (int, float)) else "Not available from LogicMonitor",
                          "Neighbor Events": v.get("neighborEvents"), "Restarts": v.get("NeighborRestart"), "Retransmit Queue": v.get("retransQueueSize")})

    # --- EIGRP peers (datasource may be applied but empty) ---
    eigrp_rows = []
    for inst, data in results["Cisco_EIGRP_Peers"][1]:
        v = latest(data)
        eigrp_rows.append({"Peer": inst.get("displayName") or inst.get("wildValue"), "Interface": inst.get("description"),
                           "Uptime (s)": v.get("peerUpTime") or v.get("Uptime"), "SRTT": v.get("peerSRTT") or v.get("SRTT"),
                           "Queue": v.get("peerQCount") or v.get("QCount"), "Retransmits": v.get("peerRetrans")})

    # --- IP / routing stats (no dedicated static-route datasource exists in this tenant) ---
    ipstat_rows = []
    for inst, data in results["System Level IP Stats-"][1]:
        v = latest(data)
        ipstat_rows.append({"Table": inst.get("displayName") or "IP", "In Datagrams/s": v.get("InDatagrams"), "Out Datagrams/s": v.get("OutDatagrams"),
                            "Reassembly OK": v.get("ReasmOK"), "Reassembly Fail": v.get("ReasmFail"), "Frag Created": v.get("OutFragCreates")})

    # --- inventory & neighbours ---
    inventory_rows = []
    for inst, _ in results["Device_Component_Inventory"][1]:
        description = inst.get("description")
        inventory_rows.append({"Component": inst.get("wildAlias") or inst.get("name"), "Description": description, "Serial Number": inst.get("displayName") or inst.get("wildValue")})
    neighbor_rows = []
    for proto in ("LLDP_Neighbors", "CDP_Neighbors"):
        for inst, _ in results[proto][1]:
            neighbor_rows.append({"Protocol": "LLDP" if proto.startswith("LLDP") else "CDP", "Local Interface": inst.get("description"),
                                  "Neighbor": inst.get("displayName") or inst.get("wildValue")})

    # --- routing coverage matrix (honest per-device availability of each protocol) ---
    def cov(name, count):
        if by_name.get(name) is None:
            return "Not monitored"
        return "Monitored" if count else "Applied · no instances"
    routing_coverage = [
        {"protocol": "BGP", "source": "BGP-", "status": cov("BGP-", len(bgp_rows)), "peers": len(bgp_rows)},
        {"protocol": "OSPF", "source": "OSPF_Neighbors", "status": cov("OSPF_Neighbors", len(ospf_rows)), "peers": len(ospf_rows)},
        {"protocol": "EIGRP", "source": "Cisco_EIGRP_Peers", "status": cov("Cisco_EIGRP_Peers", len(eigrp_rows)), "peers": len(eigrp_rows)},
        {"protocol": "Static routes", "source": "—", "status": "Not monitored", "peers": 0},
        {"protocol": "IP routing stats", "source": "System Level IP Stats-", "status": cov("System Level IP Stats-", len(ipstat_rows)), "peers": len(ipstat_rows)},
    ]

    # --- status (informational only; never used in any company metric) ---
    if remote.get("hostStatus") not in ("normal", None):
        status = "Critical"
    elif (isinstance(loss, (int, float)) and loss >= 2) or (cpu is not None and cpu >= 85) or (memory is not None and memory >= 85):
        status = "Warning"
    elif reachability is None:
        status = "Unknown"
    else:
        status = "Healthy"

    model = props.get("auto.endpoint.model") or props.get("system.model")
    details = {
        "model": model, "reachability_24h": reachability, "ping": {"loss": loss, "average": ping_now.get("average"), "max": ping_now.get("maxrtt")},
        "counts": {"interfaces": len(interface_rows), "interfaces_up": sum(1 for r in interface_rows if r["Status"] == "up"),
                   "bgp_peers": len(bgp_rows), "bgp_established": sum(1 for r in bgp_rows if r["State"] == "established"),
                   "ospf_neighbors": len(ospf_rows), "ospf_full": sum(1 for r in ospf_rows if r["State"] == "full"),
                   "eigrp_peers": len(eigrp_rows)},
        "routing_coverage": routing_coverage,
        "tables": {"Interfaces": interface_rows, "BGP Peers": bgp_rows, "OSPF Neighbors": ospf_rows, "EIGRP Peers": eigrp_rows,
                   "IP Routing Stats": ipstat_rows, "Inventory": inventory_rows, "Neighbors": neighbor_rows},
    }
    return {"status": status, "cpu": cpu, "memory": memory, "temperature": temperature, "uptime": uptime, "reachability": reachability, "details": details}


async def _refresh_one(db: Session, row: WanRouter, actor: str) -> None:
    """Pull a fresh read-only snapshot for one router and store it inline on the row."""
    if not row.lm_device_id:
        raise ValueError("Router is not mapped to LogicMonitor")
    client = LogicMonitorClient()
    remote = client.body(await client.get(f"/santaba/rest/device/devices/{row.lm_device_id}"))
    snap = await collect_wan_router(client, row.lm_device_id, remote)
    row.status = snap["status"]; row.cpu = snap["cpu"]; row.memory = snap["memory"]
    row.temperature = snap["temperature"]; row.uptime = snap["uptime"]; row.reachability = snap["reachability"]
    row.details = snap["details"]; row.match_status = "Matched"; row.last_sync = datetime.now(timezone.utc)
    if not row.provider:
        row.site_label, row.provider = _split_name(row.display_name)


async def refresh_all() -> dict:
    """Background: refresh every enabled, mapped WAN router. Kept entirely separate from
    the Device Fleet collectors."""
    async with _refresh_lock:
        with SessionLocal() as db:
            rows = db.scalars(select(WanRouter).where(WanRouter.enabled.is_(True), WanRouter.lm_device_id.is_not(None)).order_by(WanRouter.id)).all()
            ok = fail = 0
            for row in rows:
                try:
                    await _refresh_one(db, row, "background-collector")
                    db.commit(); ok += 1
                except Exception as exc:
                    db.rollback(); fail += 1
                    logger.warning("WAN router refresh failed for id %s (%s)", row.id, type(exc).__name__)
            return {"requested": len(rows), "collected": ok, "failures": fail}


async def wan_refresh_loop() -> None:
    interval = max(1, get_settings().switch_collection_interval_minutes) * 60
    while True:
        await asyncio.sleep(interval)
        try:
            logger.info("Background WAN router refresh: %s", await refresh_all())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Background WAN router refresh run failed (%s)", type(exc).__name__)


async def bootstrap_wan(actor: str = "bootstrap") -> dict:
    """One-time seed: import every 'Centrilogic-' device discovered in LogicMonitor when
    the WAN table is still empty. Admins can add/remove afterward."""
    with SessionLocal() as db:
        if db.scalar(select(WanRouter).limit(1)):
            return {"seeded": 0, "reason": "already populated"}
    client = LogicMonitorClient()
    if not (client.settings.lm_portal_url and client.settings.access_id):
        return {"seeded": 0, "reason": "LogicMonitor not configured"}
    payload = await client.get("/santaba/rest/device/devices", {"size": 1000, "filter": 'displayName~"Centrilogic"'})
    items = client.body(payload).get("items", [])
    seeded = 0
    with SessionLocal() as db:
        for d in items:
            name = d.get("displayName") or d.get("name")
            if not name or db.scalar(select(WanRouter).where(WanRouter.lm_device_id == int(d["id"]))):
                continue
            site_label, provider = _split_name(name)
            db.add(WanRouter(display_name=name, provider=provider, site_label=site_label, management_ip=d.get("name"),
                             lm_device_id=int(d["id"]), match_status="Matched", created_by=actor))
            seeded += 1
        db.commit()
    logger.info("WAN bootstrap imported %s router(s)", seeded)
    if seeded:
        # Collect them right away so the page has data without waiting for the timer.
        try:
            await refresh_all()
        except Exception as exc:
            logger.warning("WAN bootstrap initial collect failed (%s)", type(exc).__name__)
    return {"seeded": seeded}


# ---------------------------------------------------------------- API schemas

class WanAdd(BaseModel):
    lm_device_id: int | None = None
    display_name: str | None = Field(default=None, max_length=255)
    management_ip: str | None = Field(default=None, max_length=64)


def _row_json(r: WanRouter) -> dict:
    c = (r.details or {}).get("counts", {})
    return {"id": r.id, "display_name": r.display_name, "provider": r.provider, "site_label": r.site_label,
            "management_ip": r.management_ip, "lm_device_id": r.lm_device_id, "match_status": r.match_status,
            "enabled": r.enabled, "status": r.status, "cpu": r.cpu, "memory": r.memory, "reachability": r.reachability,
            "uptime": r.uptime, "last_sync": r.last_sync.isoformat() if r.last_sync else None,
            "bgp_established": c.get("bgp_established"), "bgp_peers": c.get("bgp_peers"),
            "ospf_full": c.get("ospf_full"), "interfaces_up": c.get("interfaces_up"), "interfaces": c.get("interfaces")}


# ---------------------------------------------------------------- endpoints

@router.get("/overview")
def wan_overview(db: Session = Depends(session), _: dict = Depends(current_user)) -> dict:
    """Executive summary for the WAN provider estate — descriptive counts only, no SLA."""
    rows = db.scalars(select(WanRouter).where(WanRouter.enabled.is_(True)).order_by(WanRouter.display_name)).all()
    def agg(key):
        return sum((r.details or {}).get("counts", {}).get(key, 0) or 0 for r in rows)
    reach = [r.reachability for r in rows if isinstance(r.reachability, (int, float))]
    providers: dict[str, dict] = {}
    for r in rows:
        p = providers.setdefault(r.provider or "Unspecified", {"provider": r.provider or "Unspecified", "routers": 0, "bgp": 0, "ospf": 0, "healthy": 0})
        p["routers"] += 1
        p["bgp"] += (r.details or {}).get("counts", {}).get("bgp_established", 0) or 0
        p["ospf"] += (r.details or {}).get("counts", {}).get("ospf_full", 0) or 0
        p["healthy"] += 1 if r.status == "Healthy" else 0
    last = [r.last_sync for r in rows if r.last_sync]
    return {
        "routers": len(rows),
        "reachable": sum(1 for r in rows if r.status in ("Healthy", "Warning")),
        "healthy": sum(1 for r in rows if r.status == "Healthy"),
        "warning": sum(1 for r in rows if r.status == "Warning"),
        "critical": sum(1 for r in rows if r.status == "Critical"),
        "unknown": sum(1 for r in rows if r.status == "Unknown"),
        "avg_reachability": round(sum(reach) / len(reach), 3) if reach else None,
        "interfaces": agg("interfaces"), "interfaces_up": agg("interfaces_up"),
        "bgp_peers": agg("bgp_peers"), "bgp_established": agg("bgp_established"),
        "ospf_neighbors": agg("ospf_neighbors"), "ospf_full": agg("ospf_full"), "eigrp_peers": agg("eigrp_peers"),
        "providers": sorted(providers.values(), key=lambda x: x["routers"], reverse=True),
        "last_sync": max(last).isoformat() if last else None,
        "note": "WAN provider-managed routers — shown for visibility only. Not part of Medline SLA, Overview, or reports.",
    }


@router.get("/routers")
def list_routers(db: Session = Depends(session), _: dict = Depends(current_user)) -> dict:
    rows = db.scalars(select(WanRouter).order_by(WanRouter.display_name)).all()
    return {"items": [_row_json(r) for r in rows], "total": len(rows)}


@router.get("/routers/{router_id}")
def router_detail(router_id: int, db: Session = Depends(session), _: dict = Depends(current_user)) -> dict:
    r = db.get(WanRouter, router_id)
    if not r:
        raise HTTPException(404, "WAN router not found")
    data = _row_json(r)
    data["details"] = r.details or {}
    data["notes"] = r.notes
    return data


@router.get("/search")
async def search_lm(q: str, _: dict = Depends(administrator)) -> dict:
    """Admin-only: search LogicMonitor for candidate routers to add (by name or IP)."""
    q = (q or "").strip()
    if len(q) < 2:
        raise HTTPException(400, "Provide at least 2 characters to search")
    client = LogicMonitorClient()
    safe = q.replace('"', "")
    payload = await client.get("/santaba/rest/device/devices", {"size": 25, "filter": f'displayName~"{safe}"'})
    items = client.body(payload).get("items", [])
    return {"items": [{"lm_device_id": int(d["id"]), "display_name": d.get("displayName") or d.get("name"), "management_ip": d.get("name")} for d in items]}


@router.post("/routers")
async def add_router(body: WanAdd, actor: dict = Depends(administrator), db: Session = Depends(session)) -> dict:
    """Admin-only: add a WAN router by LogicMonitor id (preferred) or by name/IP lookup,
    then immediately collect it. Never writes to LogicMonitor."""
    client = LogicMonitorClient()
    remote = None
    if body.lm_device_id:
        remote = client.body(await client.get(f"/santaba/rest/device/devices/{body.lm_device_id}"))
    elif body.display_name or body.management_ip:
        found, _method = await client.find_device(body.display_name or "", body.management_ip)
        remote = found
    if not remote or not remote.get("id"):
        raise HTTPException(404, "No single LogicMonitor device matched — refine name/IP or use the LM device id")
    lm_id = int(remote["id"])
    if db.scalar(select(WanRouter).where(WanRouter.lm_device_id == lm_id)):
        raise HTTPException(409, "That LogicMonitor device is already in the WAN list")
    name = remote.get("displayName") or remote.get("name")
    site_label, provider = _split_name(name)
    row = WanRouter(display_name=name, provider=provider, site_label=site_label, management_ip=remote.get("name"),
                    lm_device_id=lm_id, match_status="Matched", created_by=actor.get("sub", "admin"))
    db.add(row); db.flush()
    try:
        await _refresh_one(db, row, actor.get("sub", "admin"))
    except Exception as exc:
        logger.warning("Initial WAN collect failed for %s (%s)", name, type(exc).__name__)
    db.add(AuditEvent(actor=actor.get("sub", "admin"), action="wan.add", target=f"wan_router:{row.id}", details={"lm_device_id": lm_id, "name": name}))
    db.commit()
    return _row_json(row)


@router.post("/routers/{router_id}/refresh")
async def refresh_router(router_id: int, actor: dict = Depends(administrator), db: Session = Depends(session)) -> dict:
    r = db.get(WanRouter, router_id)
    if not r:
        raise HTTPException(404, "WAN router not found")
    async with _refresh_lock:
        await _refresh_one(db, r, actor.get("sub", "admin"))
        db.commit()
    return _row_json(r)


@router.delete("/routers/{router_id}")
def remove_router(router_id: int, actor: dict = Depends(administrator), db: Session = Depends(session)) -> dict:
    r = db.get(WanRouter, router_id)
    if not r:
        raise HTTPException(404, "WAN router not found")
    db.add(AuditEvent(actor=actor.get("sub", "admin"), action="wan.remove", target=f"wan_router:{router_id}", details={"name": r.display_name}))
    db.delete(r); db.commit()
    return {"removed": router_id}
