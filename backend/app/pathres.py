"""Provider & Path Resilience — per-branch availability, failover posture, and best routes.

Readable by any authenticated user; SSH collection and the remediation CLI are
administrator-only. Combines three already-collected sources per branch (site):
  * SLA daily rollup  -> availability windows (rolling-7 / WTD / previous-week / YTD),
    reported both pooled (per-device) and best-path (redundancy-aware proxy: the branch
    kept a path if its best-covered device was up).
  * SSH facts (Fill gaps) -> default-gateway priority routes, CDP/LLDP neighbours,
    EtherChannel bundles, HSRP/VRRP groups. Populates once an admin pastes the DSW output.
  * LogicMonitor snapshot -> stack member count, live status.

Nothing here writes; it only reads what other collectors stored. Routing / failover
sections are honest about missing SSH data rather than guessed.
"""
from __future__ import annotations

import ipaddress

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from . import kb, resilience, sla
from .db import Device, InventoryDevice, InventoryDeviceType, Site, SlaDaily, Snapshot, SshFacts, WanRouter

WINDOWS = ("rolling_7", "wtd", "prev_week", "ytd")


def _num(x):
    try:
        return float(str(x).replace("%", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _latest_snaps(db: Session, device_ids: list[int]) -> dict[int, Snapshot]:
    """Latest Snapshot per device, in ONE query (not one SELECT per device — the naive
    per-device version was measured firing 240+ queries for a 14-branch, 99-device fleet)."""
    if not device_ids:
        return {}
    sub = (
        select(Snapshot.device_id, func.max(Snapshot.collected_at).label("mx"))
        .where(Snapshot.device_id.in_(device_ids))
        .group_by(Snapshot.device_id)
        .subquery()
    )
    rows = db.scalars(
        select(Snapshot).join(sub, (Snapshot.device_id == sub.c.device_id) & (Snapshot.collected_at == sub.c.mx))
    ).all()
    return {s.device_id: s for s in rows}


def _dsw_ssh_facts(db: Session, legacy_ids: list[int]) -> dict[int, dict]:
    """SshFacts for every branch DSW in one query instead of one SELECT per branch."""
    if not legacy_ids:
        return {}
    out = {}
    for facts in db.scalars(select(SshFacts).where(SshFacts.device_id.in_(legacy_ids))).all():
        if not isinstance(facts.data, dict):
            continue
        tables = facts.data.get("tables", {}) if isinstance(facts.data.get("tables"), dict) else {}
        out[facts.device_id] = {"tables": tables, "collected_at": facts.collected_at.isoformat() if facts.collected_at else None}
    return out


def _branch_windows(rows_by_device: dict[int, list], legacy_ids: list[int]) -> dict:
    """Pooled and best-path availability for each window, from ALREADY-LOADED SlaDaily rows
    (one fleet-wide query up front, aggregated here in memory) — same pattern sla.fleet_sla()
    and reports.report_model() already use, instead of a SELECT per device per window."""
    ref = sla.today_local()
    out = {}
    for kind in WINDOWS:
        start, end = sla._window_bounds(kind, ref)
        per_device = []
        for did in legacy_ids:
            drows = [r for r in rows_by_device.get(did, []) if start <= r.day <= end]
            per_device.append(sla._aggregate(drows))
        avails = [w["availability"] for w in per_device if w.get("availability") is not None]
        # pooled = sum(up)/sum(observed) across the branch (current method)
        up = sum(w.get("up_minutes", 0) for w in per_device)
        obs = sum(w.get("observed_minutes", 0) for w in per_device)
        pooled = round(100.0 * up / obs, 4) if obs else None
        # best-path proxy = the best-covered device kept a path
        best = round(max(avails), 4) if avails else None
        out[kind] = {
            "start": start.isoformat(), "end": end.isoformat(),
            "pooled": pooled, "best_path": best,
            "status": "ok" if avails else "Insufficient evidence",
        }
    return out


_ETHER_UP = ("SU", "RU")  # in-use: S=Layer2/R=Layer3 bundle, U=in-use (routed port-channels use "RU", not just "SU")


def _failover(tables: dict, stack_members: int) -> dict:
    """Derive the branch's redundancy/failover posture from the collected tables."""
    default_gw = tables.get("Default Gateway", []) or []
    fhrp = tables.get("Gateway Redundancy", []) or []
    ether_all = tables.get("EtherChannel", []) or []
    ether = [r for r in ether_all if str(r.get("Status", "")).startswith(_ETHER_UP)]
    ether_down = [r for r in ether_all if str(r.get("Status", "")).strip() and not str(r.get("Status", "")).startswith(_ETHER_UP)]
    neighbors = tables.get("CDP-LLDP Neighbors", []) or []

    # access dual-homing: a neighbour that appears on 2+ local interfaces is dual-homed
    by_neighbor: dict[str, set] = {}
    for r in neighbors:
        name = str(r.get("Neighbor") or "").split(".")[0].lower()
        if name:
            by_neighbor.setdefault(name, set()).add(str(r.get("Local Interface") or ""))
    dual_homed = sum(1 for ifaces in by_neighbor.values() if len(ifaces) >= 2)

    nexthops = {str(r.get("Next Hop")) for r in default_gw if r.get("Next Hop")}
    northbound_paths = len(nexthops)

    dist_redundant = stack_members >= 2
    access_redundant = dual_homed > 0 or len(ether) > 0
    # FHRP (HSRP/VRRP) protects the LAN-side default gateway for downstream hosts between two
    # local chassis — it says nothing about whether the WAN uplink itself has a second path.
    # Northbound/WAN redundancy must be judged on actual route diversity, not FHRP presence,
    # or a branch with HSRP on its access VLAN but a single WAN path reads as "fully redundant".
    north_redundant = northbound_paths >= 2

    findings = []
    if default_gw:
        if northbound_paths <= 1:
            findings.append({"severity": "warning", "text": (
                f"Single northbound Layer-3 path (default route via {next(iter(nexthops), '?')}, no floating "
                "backup). One upstream failure isolates the branch." + (
                    " HSRP/VRRP is present but only protects the local LAN gateway, not the WAN path." if fhrp else ""))})
        else:
            findings.append({"severity": "ok", "text": f"{northbound_paths} default-route next-hop(s) present."})
    if stack_members >= 2:
        findings.append({"severity": "ok", "text": f"Distribution is a {stack_members}-member stack (supervisor redundancy)."})
    elif stack_members == 1:
        findings.append({"severity": "warning", "text": "Distribution is a single (non-stacked) switch — no supervisor redundancy."})
    if access_redundant:
        findings.append({"severity": "ok", "text": f"{dual_homed} access switch(es) dual-homed / {len(ether)} EtherChannel bundle(s) up."})
    else:
        findings.append({"severity": "warning", "text": "No dual-homed access switches and no EtherChannel bundles up — a single link/port failure can isolate an access switch."})
    if ether_down:
        findings.append({"severity": "warning", "text": f"{len(ether_down)} EtherChannel bundle(s) configured but not in use (down/suspended) — check for a one-sided LACP mode or allowed-VLAN mismatch."})
    if fhrp:
        no_preempt = [r for r in fhrp if str(r.get("Preempt") or "").strip().lower() not in ("p", "yes", "enabled", "true")]
        if no_preempt:
            findings.append({"severity": "warning", "text": f"{len(no_preempt)} HSRP/VRRP group(s) without preempt — the recovering chassis won't reclaim active role after a failover, so traffic can keep riding the unintended path."})

    return {
        "distribution_redundant": dist_redundant,
        "access_redundant": access_redundant,
        "northbound_redundant": north_redundant,
        "northbound_paths": northbound_paths,
        "dual_homed_access": dual_homed,
        "access_neighbors": len(by_neighbor),
        "etherchannels_up": len(ether),
        "etherchannels_down": len(ether_down),
        "fhrp_groups": len(fhrp),
        "priority_routes": [
            {"priority": r.get("Priority"), "prefix": r.get("Prefix"), "next_hop": r.get("Next Hop"),
             "protocol": r.get("Protocol"), "ad_metric": r.get("Distance/Metric")}
            for r in sorted(default_gw, key=lambda x: x.get("Priority") or 99)
        ],
        "has_routing_evidence": bool(default_gw),
        "findings": findings,
    }


def _wan_circuits(all_routers: list[WanRouter], dsw_mgmt_ip: str | None, default_nexthops: set[str]) -> list[dict]:
    """Map the branch to its WAN provider circuits: any WAN router whose management IP sits in
    the branch DSW's local /24 (the transit subnets), flagging the one that is the L3 default
    next-hop as primary. This is the 'provider next-hop' mapping. `all_routers` is loaded ONCE
    for the whole payload by the caller (was a fresh full-table SELECT per branch before)."""
    if not dsw_mgmt_ip:
        return []
    try:
        net = ipaddress.ip_network(f"{dsw_mgmt_ip}/24", strict=False)
    except ValueError:
        return []
    out = []
    for r in all_routers:
        ip = r.management_ip or ""
        try:
            inside = ipaddress.ip_address(ip) in net
        except ValueError:
            inside = False
        if inside:
            out.append({
                "provider": (r.provider or "").strip() or (r.site_label or "circuit"),
                "site_label": r.site_label, "ip": ip, "status": r.status or "Unknown",
                "is_primary": ip in default_nexthops,
            })
    out.sort(key=lambda c: (not c["is_primary"], c["provider"]))
    return out


def build(db: Session, site_code: str | None = None, include_commands: bool = True) -> dict:
    """One payload: every branch (or a single site_code) with availability + failover + routes.

    `include_commands` is False for non-administrators: the remediation findings are still
    returned (leadership needs the risk), but the device CLI is stripped server-side."""
    q = (
        select(InventoryDevice, InventoryDeviceType.type_code, Site.site_code, Site.city, Site.province_region)
        .join(InventoryDeviceType, InventoryDeviceType.id == InventoryDevice.device_type_id)
        .join(Site, Site.id == InventoryDevice.site_id)
        .where(InventoryDevice.enabled.is_(True), InventoryDevice.logicmonitor_device_id.is_not(None))
    )
    if site_code:
        q = q.where(Site.site_code == site_code)
    rows = db.execute(q).all()

    # map inventory -> legacy Device (SLA/snapshots are keyed by legacy id)
    legacy_by_lm = {d.lm_device_id: d for d in db.scalars(select(Device).where(Device.lm_device_id.is_not(None))).all()}
    all_legacy_ids = [legacy_by_lm[inv.logicmonitor_device_id].id for inv, *_ in rows if inv.logicmonitor_device_id in legacy_by_lm]

    # Batch every per-device/per-branch lookup ONCE for the whole payload (was N+1: a fresh
    # SELECT per device for snapshots, per device per window for SLA rows, per branch for SSH
    # facts, and a full-table scan per branch for WAN routers — measured at 242 queries for a
    # 14-branch fleet before this fix).
    snap_by_id = _latest_snaps(db, all_legacy_ids)
    ref = sla.today_local()
    ytd_start, _ = sla._window_bounds("ytd", ref)
    rows_by_device: dict[int, list] = {}
    if all_legacy_ids:
        for r in db.scalars(select(SlaDaily).where(SlaDaily.device_id.in_(all_legacy_ids), SlaDaily.day >= ytd_start)).all():
            rows_by_device.setdefault(r.device_id, []).append(r)
    all_wan_routers = db.scalars(select(WanRouter)).all()

    branches: dict[str, dict] = {}
    for inv, type_code, sc, city, region in rows:
        legacy = legacy_by_lm.get(inv.logicmonitor_device_id)
        if not legacy:
            continue
        b = branches.setdefault(sc, {"site_code": sc, "city": city, "region": region, "devices": [], "_dsw": None, "_dsw_ip": None, "_ids": []})
        snap = snap_by_id.get(legacy.id)
        # Canonical physical-chassis count (same function Device Fleet's "Total Network
        # Devices" and Overview's Site Reliability table use) so this branch's device count
        # agrees with those pages instead of a locally-invented estimate.
        counted, _src = resilience.physical_switch_count(snap.details) if snap else (None, None)
        physical = counted or 1
        dev = {
            "name": inv.generated_name, "type": type_code, "role": inv.role,
            "management_ip": inv.management_ip, "legacy_id": legacy.id,
            "status": snap.status if snap else "Unknown", "model": inv.model,
            "physical": physical,                      # chassis in this logical unit
            "is_stack": bool(counted and counted > 1),  # a stack reports to LM as ONE device
            "stack_confirmed": counted is not None,     # False = stack size not yet evidenced
        }
        b["devices"].append(dev)
        b["_ids"].append(legacy.id)
        if type_code in ("DSW", "DAS") and b["_dsw"] is None:
            b["_dsw"] = legacy.id
            b["_dsw_ip"] = inv.management_ip

    dsw_ids = [b["_dsw"] or (b["_ids"][0] if b["_ids"] else None) for b in branches.values()]
    dsw_ids = [d for d in dsw_ids if d is not None]
    ssh_by_dsw = _dsw_ssh_facts(db, dsw_ids)

    out = []
    for sc, b in sorted(branches.items()):
        dsw_id = b["_dsw"] or (b["_ids"][0] if b["_ids"] else None)
        ssh = ssh_by_dsw.get(dsw_id, {}) if dsw_id else {}
        ssh_tables = ssh.get("tables", {})
        # LM snapshot already carries CDP/LLDP neighbours + stack member count for the DSW.
        lm_tables, stack_members = {}, 1
        if dsw_id:
            snap = snap_by_id.get(dsw_id)
            if snap and isinstance(snap.details, dict):
                lm_tables = snap.details.get("tables", {}) or {}
                count, _src = resilience.physical_switch_count(snap.details)
                stack_members = count if isinstance(count, int) and count else 1
        # SSH fills what LM can't (routing/HSRP/EtherChannel); SSH wins where both exist.
        tables = {**lm_tables, **ssh_tables}
        windows = _branch_windows(rows_by_device, b["_ids"])
        failover = _failover(tables, stack_members)
        # WAN provider circuits mapped by default next-hop / transit subnet
        default_nexthops = {r.get("Next Hop") for r in (tables.get("Default Gateway", []) or []) if r.get("Next Hop")}
        wan = _wan_circuits(all_wan_routers, b.get("_dsw_ip"), default_nexthops)
        failover["wan_circuits"] = len(wan)
        failover["wan_redundant"] = len(wan) >= 2
        # overall posture label
        ew_redundant = failover["access_redundant"] and failover["distribution_redundant"]
        wan_note = f"; {len(wan)} WAN circuit(s)" if wan else ""
        if not failover["has_routing_evidence"]:
            posture = ("East-west redundant; northbound needs SSH" if ew_redundant
                       else "Run Fill gaps on the DSW to assess")
        elif ew_redundant and (failover["northbound_redundant"] or failover["wan_redundant"]):
            posture = f"Fully redundant ({len(wan)} WAN circuits)" if wan else "Fully redundant"
        elif ew_redundant:
            posture = "Redundant east-west, single L3 default" + wan_note
        else:
            posture = "Partial redundancy"
        if wan:
            provs = ", ".join(f"{c['provider']}{' (primary)' if c['is_primary'] else ''}" for c in wan)
            sev = "ok" if len(wan) >= 2 else "warning"
            failover.setdefault("findings", []).append({"severity": sev, "text": f"WAN circuits mapped: {provs}."})
        # Device counting, stated unambiguously because a switch stack is several physical
        # switches that LogicMonitor (correctly) monitors as ONE logical device:
        #   physical_switches = chassis, the SAME canonical figure as Device Fleet's "Total
        #                       Network Devices" and Overview's Site Reliability "Devices"
        #                       (backend.app.inventory.physical_switch_count)
        #   monitored_units   = logical devices polled by LogicMonitor
        # Every unit reaching this point is mapped AND collected, so all of them are monitored;
        # a smaller monitored_units than physical_switches means "stacked", never "unmonitored".
        monitored = len(b["_ids"])
        device_total = sum(d["physical"] for d in b["devices"])
        stacks = sum(1 for d in b["devices"] if d["is_stack"])
        unconfirmed = sum(1 for d in b["devices"] if not d["stack_confirmed"])
        out.append({
            "site_code": sc, "city": b["city"], "region": b["region"],
            "device_count": device_total, "monitored_count": monitored, "devices": b["devices"],
            "physical_switches": device_total, "monitored_units": monitored,
            "stack_count": stacks, "standalone_count": monitored - stacks,
            "stack_size_unconfirmed": unconfirmed, "all_monitored": True,
            "dsw_device_id": b["_dsw"],  # legacy device id for the admin SSH-pull button
            "windows": windows, "failover": failover, "posture": posture,
            "ssh_collected_at": ssh.get("collected_at"),
            "wan_circuits": wan,
            "kb": kb.for_branch(failover, tables, include_commands),  # findings; CLI for admins only
        })
    return {"branches": out, "as_of": sla.today_local().isoformat()}
