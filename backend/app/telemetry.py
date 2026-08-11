"""Network telemetry — routing (OSPF), interface/circuit health, latency/loss, and an
honest Data Coverage matrix. Fleet-scoped, read from stored snapshots (no live LM on load).
Grounded in the LM discovery: BGP/EIGRP/static/IP-SLA-jitter are NOT MONITORED for this
tenant; OSPF is real on the OSPF-enabled subset; interfaces + Ping latency/loss are real.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import overview


def _num(v):
    try:
        n = float(v)
        return n if n == n and n not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _tables(d):
    snap = d["snap"]
    return (snap.details or {}).get("tables", {}) if snap and isinstance(snap.details, dict) else {}


def routing(db: Session, fleet):
    items = []
    for d in fleet:
        rows = _tables(d).get("OSPF Neighbors", []) or []
        if not rows:
            continue
        full = sum(1 for r in rows if str(r.get("State")) == "full")
        events = sum(int(_num(r.get("Neighbor Events")) or 0) for r in rows)
        items.append({
            "device_id": d["device_id"], "hostname": d["hostname"], "city": d["city"],
            "neighbors": len(rows), "full": full, "neighbor_events": events,
            "status": "HEALTHY" if full == len(rows) and events == 0 else ("DEGRADED" if full < len(rows) else "WARNING"),
            "peers": [{"neighbor": r.get("Neighbor"), "state": r.get("State"), "events": r.get("Neighbor Events")} for r in rows],
        })
    items.sort(key=lambda x: (x["full"] == x["neighbors"], x["hostname"]))
    return {"protocol": "OSPF", "devices_with_ospf": len(items), "fleet": len(fleet), "items": items,
            "bgp": "Not monitored", "eigrp": "Not monitored", "static": "Not available"}


def interfaces(db: Session, fleet):
    total = up = down = disabled = unknown = high_util = with_errors = flapping = 0
    issues = []
    for d in fleet:
        for r in _tables(d).get("Interfaces", []) or []:
            total += 1
            st = str(r.get("Status")); adm = str(r.get("Admin State"))
            util = max(_num(r.get("In Utilization %")) or 0, _num(r.get("Out Utilization %")) or 0)
            tx, rx = _num(r.get("TX Errors")), _num(r.get("RX Errors"))
            flaps = _num(r.get("Flaps"))
            is_down_problem = st == "down" and adm == "up"
            # Every interface lands in exactly one state bucket, so total always equals
            # up + down + disabled + unknown - nothing silently disappears from the counts.
            # "unknown" is common: LogicMonitor discovers far more interface instances than it
            # actively polls operational status for (commonly unused/never-connected ports), so
            # Status/Admin State come back as "unknown" rather than a fabricated up or down.
            if st == "up":
                up += 1
            elif is_down_problem:
                down += 1
            elif adm == "down":
                disabled += 1
            else:
                unknown += 1
            if is_down_problem:
                issues.append({"device_id": d["device_id"], "device": d["hostname"], "interface": r.get("Interface"), "issue": "DOWN (admin up)", "severity": "CRITICAL"})
            elif util >= 90:
                high_util += 1; issues.append({"device_id": d["device_id"], "device": d["hostname"], "interface": r.get("Interface"), "issue": f"{round(util)}% utilisation", "severity": "WARNING"})
            elif (tx and tx > 0) or (rx and rx > 0):
                with_errors += 1
            if util >= 80 and util < 90:
                high_util += 1
            if flaps and flaps > 0:
                flapping += 1
    return {"total": total, "up": up, "down": down, "disabled": disabled, "unknown": unknown,
            "high_util": high_util, "with_errors": with_errors, "flapping": flapping, "issues": issues[:25]}


def latency(db: Session, fleet):
    lats, losses = [], []
    for d in fleet:
        snap = d["snap"]
        ping = (snap.details or {}).get("ping", {}) if snap and isinstance(snap.details, dict) else {}
        a, l = _num(ping.get("average")), _num(ping.get("loss"))
        if a is not None:
            lats.append(a)
        if l is not None:
            losses.append(l)
    return {"avg_latency_ms": round(sum(lats) / len(lats), 2) if lats else None,
            "max_latency_ms": round(max(lats), 2) if lats else None,
            "avg_loss_pct": round(sum(losses) / len(losses), 3) if losses else None,
            "devices_reporting": len(lats), "jitter": "Not monitored"}


def data_coverage(db: Session, fleet):
    ospf_n = sum(1 for d in fleet if _tables(d).get("OSPF Neighbors"))
    intf_n = sum(1 for d in fleet if _tables(d).get("Interfaces"))
    total = len(fleet)
    rows = [
        ("Availability / uptime", "Ping · HostStatus · SNMP_Host_Uptime", "Monitored", total),
        ("Latency & packet loss", "Ping (rtt · PingLossPercent)", "Monitored", total),
        ("Jitter", "CiscoSLA_ICMPJitter", "Not monitored", 0),
        ("CPU / memory / temperature", "Cisco_CPU_SNMP · MemoryPools · Sensors", "Monitored", total),
        ("Power / fans / stack", "PowerSupplies · CiscoFan · Switch Stack", "Monitored", total),
        ("Interfaces (status/util/errors)", "SNMP_Network_Interfaces", "Monitored", intf_n),
        ("OSPF adjacency", "OSPF_Neighbors", "Partial" if ospf_n else "Not monitored", ospf_n),
        ("BGP peers / routes", "BGP-", "Not monitored", 0),
        ("EIGRP", "Cisco_EIGRP_*", "Not monitored", 0),
        ("Static / route-table counts", "(no datasource)", "Not available", 0),
        ("Config backup", "LogicMonitor_ConfigSource_Metrics", "Monitored", total),
        ("CDP/LLDP topology", "CDP_Neighbors · LLDP_Neighbors", "Monitored", total),
    ]
    return {"fleet": total, "metrics": [{"metric": m, "source": s, "status": st, "devices": n} for (m, s, st, n) in rows]}


def build(db: Session) -> dict:
    fleet = overview._fleet(db)
    return {
        "data_coverage": data_coverage(db, fleet),
        "routing": routing(db, fleet),
        "interfaces": interfaces(db, fleet),
        "latency": latency(db, fleet),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
