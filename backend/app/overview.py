"""Executive Overview aggregation — Fleet-scoped, computed from stored evidence only.

Scope rule: only devices registered in Device Fleet (mapped `Device` rows) are ever
analyzed. Nothing here queries LogicMonitor live; everything derives from the snapshot,
sla_daily and resilience stores that the background collectors already maintain.
Missing evidence is reported as INSUFFICIENT EVIDENCE / NOT MONITORED / UNKNOWN — never
fabricated as 0% or 100%.
"""
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import resilience, sla
from .config import get_settings
from .db import Device, InventoryDevice, ResilienceAssessment, Site, SlaDaily, Snapshot


def _band(criticality: str) -> str:
    c = (criticality or "").lower()
    if c == "critical":
        return "Critical"
    if c == "high":
        return "High"
    return "Standard"


def _health(snap) -> str:
    return snap.status if snap and snap.status else "Unknown"


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _fleet(db: Session) -> list[dict]:
    devices = sla.monitored_devices(db)
    inv = {i.logicmonitor_device_id: i for i in db.scalars(select(InventoryDevice).where(InventoryDevice.logicmonitor_device_id.is_not(None))).all()}
    sites = {s.id: s for s in db.scalars(select(Site)).all()}
    fleet = []
    for d in devices:
        snap = db.scalar(select(Snapshot).where(Snapshot.device_id == d.id).order_by(Snapshot.collected_at.desc()).limit(1))
        i = inv.get(d.lm_device_id)
        site = sites.get(i.site_id) if i else None
        criticality = (i.criticality if i else d.criticality) or "Medium"
        fleet.append({
            "device_id": d.id, "hostname": d.hostname, "lm_device_id": d.lm_device_id,
            "match_status": d.match_status, "model": d.model,
            "criticality": criticality, "band": _band(criticality),
            "site_code": site.site_code if site else None,
            "city": (site.city if site else d.site) or "Unassigned",
            "province": (site.province_region if site else "") or "",
            "business_unit": (getattr(site, "business_unit", None) or "Unassigned"),
            "status": _health(snap), "snap": snap,
            "collected_at": snap.collected_at if snap else None,
        })
    return fleet


# ---- fleet SLA windows from the daily rollup ----

def _fleet_window(db: Session, device_ids: list[int], start: date, end: date) -> dict:
    if not device_ids:
        return {"availability": None, "coverage": 0.0, "status": "Insufficient evidence"}
    rows = db.scalars(select(SlaDaily).where(SlaDaily.device_id.in_(device_ids), SlaDaily.day >= start, SlaDaily.day <= end)).all()
    return sla._aggregate(rows)


def header(db: Session, fleet: list[dict]) -> dict:
    settings = get_settings()
    matched = sum(1 for d in fleet if d["match_status"] == "Matched")
    last_sync = max((d["collected_at"] for d in fleet if d["collected_at"]), default=None)
    fleet_ids = [d["device_id"] for d in fleet]
    ytd = _fleet_window(db, fleet_ids, *sla._window_bounds("ytd", sla.today_local()))
    coverage = ytd["coverage"]
    age_min = ((datetime.now(timezone.utc) - last_sync).total_seconds() / 60) if last_sync else None
    stale = age_min is not None and age_min > max(60, settings.stale_minutes * 2)
    quality = "High" if coverage >= 95 and not stale else ("Medium" if coverage >= 80 and not stale else "Low")
    return {
        "org": "MEDLINE CANADA", "title": "Enterprise Network Reliability",
        "api_status": "Connected" if settings.lm_portal_url else "Not configured",
        "database_status": "Connected",
        "last_sync": last_sync.isoformat() if last_sync else None,
        "data_stale": stale, "data_age_minutes": round(age_min) if age_min is not None else None,
        "fleet_coverage": {"monitored": len(fleet), "total": len(fleet)},
        "lm_coverage": {"matched": matched, "total": len(fleet)},
        "sla_evidence_coverage": round(coverage, 1),
        "data_quality": quality,
    }


def global_sla(db: Session, fleet: list[dict]) -> dict:
    settings = get_settings()
    ids = [d["device_id"] for d in fleet]
    ref = sla.today_local()
    d30 = _fleet_window(db, ids, *sla._window_bounds("rolling_30", ref))
    d7 = _fleet_window(db, ids, ref - _delta(7), ref)
    ytd = _fleet_window(db, ids, *sla._window_bounds("ytd", ref))
    current = d30["availability"]
    target = settings.sla_target
    delta = round(current - target, 4) if current is not None else None
    if current is None:
        status = "Insufficient evidence"
    elif current >= target:
        status = "Target met"
    elif current >= target - 0.05:
        status = "At risk"
    else:
        status = "Below target"
    return {"current": current, "target": target, "delta": delta, "status": status,
            "window": "30-day", "trend_7d": d7["availability"], "trend_30d": d30["availability"],
            "ytd": ytd["availability"], "coverage": d30["coverage"]}


def _delta(days):
    from datetime import timedelta
    return timedelta(days=days)


def criticality(db: Session, fleet: list[dict]) -> dict:
    bands = {"Critical": 0, "High": 0, "Standard": 0}
    degraded = unreachable = 0
    for d in fleet:
        bands[d["band"]] += 1
        s = d["status"]
        if s == "Warning":
            degraded += 1
        elif s in ("Critical", "Unknown") or d["match_status"] != "Matched":
            unreachable += 1
    total = len(fleet)
    return {"total": total, "bands": bands,
            "percentages": {k: (round(100 * v / total, 1) if total else 0) for k, v in bands.items()},
            "degraded": degraded, "unreachable": unreachable}


def availability_series(db: Session, fleet: list[dict], days: int = 7) -> dict:
    settings = get_settings()
    ids = [d["device_id"] for d in fleet]
    ref = sla.today_local()
    series = []
    for n in range(days - 1, -1, -1):
        day = ref - _delta(n)
        rows = db.scalars(select(SlaDaily).where(SlaDaily.device_id.in_(ids), SlaDaily.day == day)).all() if ids else []
        agg = sla._aggregate(rows)
        cause = None
        avail = agg["availability"]
        abnormal = avail is not None and avail < settings.sla_target
        if abnormal and rows:
            worst = min((r for r in rows if r.availability is not None), key=lambda r: r.availability, default=None)
            if worst is not None:
                host = db.scalar(select(Device.hostname).where(Device.id == worst.device_id))
                cause = f"{host}: {round(worst.availability, 3)}% availability"
        series.append({"day": day.isoformat(), "availability": avail, "coverage": agg["coverage"],
                       "abnormal": bool(abnormal), "cause": cause, "status": agg["status"]})
    return {"days": days, "target": settings.sla_target, "series": series}


def _device_problem(d: dict):
    """Return (severity 0-3, metric_label, metric_value, status) for a fleet device now."""
    snap = d["snap"]
    if snap is None or d["status"] == "Unknown" or d["match_status"] != "Matched":
        return 3 if d["band"] == "Critical" else 2, "Reachability", "No recent evidence", "UNKNOWN"
    cpu, mem, temp = _num(snap.cpu), _num(snap.memory), _num(snap.temperature)
    tables = (snap.details or {}).get("tables", {}) if isinstance(snap.details, dict) else {}
    env = tables.get("Environmental and PoE", []) or []
    hw_fault = any(str(r.get("State")) == "Fault" for r in env)
    if snap.status == "Critical" or hw_fault:
        label, value = ("Hardware", "Fault") if hw_fault else ("Health", "Critical")
        return 3, label, value, "CRITICAL"
    if cpu is not None and cpu >= 85:
        return 2, "CPU", f"{round(cpu)}%", "HIGH CPU"
    if mem is not None and mem >= 85:
        return 2, "Memory", f"{round(mem)}%", "HIGH MEM"
    if temp is not None and temp >= 65:
        return 2, "Max Temp", f"{round(temp)}C", "HIGH TEMP"
    if snap.status == "Warning":
        return 2, "Health", "Warning", "WARNING"
    uptime = (snap.details or {}).get("uptime") if isinstance(snap.details, dict) else None
    return 0, "Uptime", sla_uptime(uptime), "OK"


def sla_uptime(seconds):
    n = _num(seconds)
    if n is None:
        return "—"
    days = int(n // 86400)
    return f"{days}d"


def core_infrastructure(db: Session, fleet: list[dict], limit: int = 5) -> list[dict]:
    weight = {"Critical": 3, "High": 2, "Standard": 1}
    ranked = []
    for d in fleet:
        sev, label, value, status = _device_problem(d)
        score = sev * 10 + weight[d["band"]]
        ranked.append({"device_id": d["device_id"], "hostname": d["hostname"], "city": d["city"],
                       "model": d["model"] or "—", "metric": label, "value": value, "status": status,
                       "band": d["band"], "_score": score, "_sev": sev})
    ranked.sort(key=lambda x: (-x["_score"], x["hostname"]))
    top = [x for x in ranked if x["_sev"] > 0][:limit]
    if len(top) < limit:  # backfill with healthiest criticals so the panel is never empty
        top += [x for x in ranked if x["_sev"] == 0][: limit - len(top)]
    for x in top:
        x.pop("_score", None); x.pop("_sev", None)
    return top


def site_reliability(db: Session, fleet: list[dict]) -> list[dict]:
    ref = sla.today_local()
    groups: dict[str, list[dict]] = {}
    for d in fleet:
        groups.setdefault(d["site_code"] or d["city"], []).append(d)
    rows = []
    for key, members in sorted(groups.items(), key=lambda kv: kv[0] or ""):
        ids = [m["device_id"] for m in members]
        avail = _fleet_window(db, ids, *sla._window_bounds("ytd", ref))
        avail30 = _fleet_window(db, ids, *sla._window_bounds("rolling_30", ref))
        statuses = [m["status"] for m in members]
        incidents = sum(1 for s in statuses if s in ("Critical", "Unknown"))
        degraded = sum(1 for s in statuses if s == "Warning")
        worst = "CRITICAL" if incidents else ("DEGRADED" if degraded else ("HEALTHY" if avail["availability"] is not None else "INSUFFICIENT EVIDENCE"))
        first = members[0]
        rows.append({
            "site_code": first["site_code"], "city": first["city"], "province": first["province"],
            "business_unit": first["business_unit"], "devices": len(members),
            "availability_ytd": avail["availability"], "availability_30d": avail30["availability"],
            "coverage": avail["coverage"], "device_health": worst,
            "critical_devices": incidents, "degraded_devices": degraded,
            "criticality": max((m["band"] for m in members), key=lambda b: {"Critical": 3, "High": 2, "Standard": 1}[b]),
            "status": worst,
        })
    return rows


def throughput(db: Session, fleet: list[dict]) -> dict:
    # LogicMonitor exposes interface utilisation/speed but not a role tag; summing every
    # interface would double-count access/distribution/core. Executive aggregate throughput
    # needs uplink/WAN interface tagging (Phase B), so we report it honestly rather than fake it.
    return {"available": False, "reason": "Requires uplink/WAN interface role mapping (Phase B).", "value": None, "unit": None}


def executive_actions(db: Session, fleet: list[dict], gsla: dict) -> list[dict]:
    actions = []
    for d in fleet:
        sev, label, value, status = _device_problem(d)
        if status == "UNKNOWN" and d["band"] in ("Critical", "High"):
            actions.append({"priority": 1, "severity": "P1", "title": f"Restore monitoring evidence for {d['hostname']}",
                            "detail": f"{d['hostname']} ({d['city']}) has no recent LogicMonitor evidence; reliability cannot be confirmed for a {d['band'].lower()}-criticality device.",
                            "device_id": d["device_id"], "_rank": 100 + {"Critical": 3, "High": 2}.get(d["band"], 1)})
        elif status == "CRITICAL":
            actions.append({"priority": 1, "severity": "P1", "title": f"Investigate critical condition on {d['hostname']}",
                            "detail": f"{d['hostname']} ({d['city']}) reports {label} {value}. Validate hardware/environment before it affects site availability.",
                            "device_id": d["device_id"], "_rank": 90 + {"Critical": 3, "High": 2, "Standard": 1}[d["band"]]})
        elif status in ("HIGH CPU", "HIGH MEM", "HIGH TEMP"):
            actions.append({"priority": 2, "severity": "P2", "title": f"Address capacity risk on {d['hostname']}",
                            "detail": f"{d['hostname']} ({d['city']}) {label} at {value} and trending toward its warning threshold.",
                            "device_id": d["device_id"], "_rank": 50 + {"Critical": 3, "High": 2, "Standard": 1}[d["band"]]})
    latest = resilience.latest_assessment(db)
    for dev in (latest.get("devices") or []):
        if dev.get("tier") == "Tier I" and dev.get("score", 0) <= 1:
            actions.append({"priority": 2, "severity": "P2", "title": f"Reduce single-point-of-failure exposure at {dev['hostname']}",
                            "detail": f"{dev['hostname']} shows no redundant uplink/stack/power path (Tier I-equivalent). Availability may be healthy today but a single failure removes the site path.",
                            "device_id": dev.get("device_id"), "_rank": 40})
    if gsla["current"] is not None and gsla["current"] < gsla["target"]:
        actions.append({"priority": 2, "severity": "P2", "title": "Fleet SLA below target",
                        "detail": f"30-day network availability is {gsla['current']:.3f}% against a {gsla['target']}% target. Review the lowest-availability sites.",
                        "device_id": None, "_rank": 45})
    actions.sort(key=lambda a: -a["_rank"])
    seen, out = set(), []
    for a in actions:
        if a["title"] in seen:
            continue
        seen.add(a["title"]); a.pop("_rank", None); out.append(a)
        if len(out) >= 5:
            break
    for i, a in enumerate(out, 1):
        a["priority"] = i
    return out


def executive_summary(db: Session, fleet: list[dict], gsla: dict, crit: dict, sites: list[dict], header_data: dict) -> str:
    total = len(fleet)
    if gsla["current"] is None:
        return (f"Fleet telemetry coverage is {header_data['sla_evidence_coverage']}% across {total} devices, which is below the "
                f"90% evidence threshold, so a governed network SLA cannot yet be published. Continue collection to establish the baseline.")
    verb = "remained above target" if gsla["current"] >= gsla["target"] else "fell below target"
    worst = min((s for s in sites if s["availability_ytd"] is not None), key=lambda s: s["availability_ytd"], default=None)
    parts = [f"Canada network availability {verb} at {gsla['current']:.3f}% over the trailing 30 days against a {gsla['target']}% objective."]
    if crit["unreachable"]:
        parts.append(f"{crit['unreachable']} of {total} devices currently lack confirmed reachability evidence and are excluded from healthy counts.")
    else:
        parts.append("All monitored devices are currently reporting.")
    if worst and worst["availability_ytd"] is not None and worst["availability_ytd"] < gsla["target"]:
        parts.append(f"The lowest-performing site is {worst['city']} at {worst['availability_ytd']:.3f}% YTD.")
    parts.append(f"Fleet telemetry coverage was {header_data['sla_evidence_coverage']}%.")
    return " ".join(parts)


def build(db: Session) -> dict:
    fleet = _fleet(db)
    hdr = header(db, fleet)
    gsla = global_sla(db, fleet)
    crit = criticality(db, fleet)
    sites = site_reliability(db, fleet)
    resil = resilience.latest_assessment(db)
    return {
        "header": hdr,
        "global_sla": gsla,
        "criticality": crit,
        "availability_7d": availability_series(db, fleet, 7),
        "throughput": throughput(db, fleet),
        "core_infrastructure": core_infrastructure(db, fleet),
        "actions": executive_actions(db, fleet, gsla),
        "sites": sites,
        "resilience": {"fleet_tier": resil.get("fleet_tier"), "as_of": resil.get("as_of")},
        "summary": executive_summary(db, fleet, gsla, crit, sites, hdr),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
