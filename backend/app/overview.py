"""Executive Overview aggregation — Fleet-scoped, computed from stored evidence only.

Scope rule: only devices registered in Device Fleet (mapped `Device` rows) are ever
analyzed. Nothing here queries LogicMonitor live; everything derives from the snapshot,
sla_daily and resilience stores that the background collectors already maintain.
Missing evidence is reported as INSUFFICIENT EVIDENCE / NOT MONITORED / UNKNOWN — never
fabricated as 0% or 100%.
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from . import resilience, sla
from .config import get_settings
from .db import Device, InventoryDevice, ResilienceAssessment, Site, SlaDaily, Snapshot
from .inventory import physical_switch_count


# Business-importance bands. These are the four values the inventory schema actually offers
# (schemas.py: Literal["Low","Medium","High","Critical"], default "Medium"). They describe how
# important a device is to the BUSINESS — they are NOT its live health. A device can be
# "Medium" criticality and simultaneously CRITICAL on Core Infrastructure Health (a faulty but
# non-essential switch), or "Critical" criticality and perfectly healthy.
BANDS = ("Critical", "High", "Medium", "Low")
BAND_WEIGHT = {"Critical": 4, "High": 3, "Medium": 2, "Low": 1}


def _band(criticality: str) -> str:
    """Normalise the stored criticality to one of BANDS.

    Previously anything that was not Critical/High was relabelled "Standard", which
    mislabelled the two distinct real values Medium and Low as a single band that does not
    exist in the schema — so the Overview chart reported "Standard 40" for a fleet whose
    devices are all actually at the default "Medium"."""
    c = (criticality or "").strip().lower()
    for b in BANDS:
        if c == b.lower():
            return b
    return "Medium"  # schema default when unset/unrecognised


def _health(snap) -> str:
    return snap.status if snap and snap.status else "Unknown"


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _fleet(db: Session) -> list[dict]:
    """The full Device Fleet = every enabled InventoryDevice, with its collected legacy
    Device/snapshot joined where the device is mapped and monitored. Devices that are
    registered but not yet collected are still counted (criticality, coverage) with an
    Unknown health, so totals reflect the whole fleet, not only the monitored subset."""
    inventory = db.scalars(select(InventoryDevice).where(InventoryDevice.enabled.is_(True))).all()
    legacy = {d.lm_device_id: d for d in db.scalars(select(Device).where(Device.lm_device_id.is_not(None))).all()}
    sites = {s.id: s for s in db.scalars(select(Site)).all()}
    # Batch-fetch the latest snapshot per device in one query (avoids an N+1 over the fleet).
    dev_ids = [d.id for d in legacy.values()]
    snap_by_dev: dict[int, Snapshot] = {}
    if dev_ids:
        newest = (select(Snapshot.device_id, func.max(Snapshot.collected_at).label("mx"))
                  .where(Snapshot.device_id.in_(dev_ids)).group_by(Snapshot.device_id).subquery())
        rows = db.scalars(select(Snapshot).join(newest, and_(Snapshot.device_id == newest.c.device_id,
                                                             Snapshot.collected_at == newest.c.mx))).all()
        snap_by_dev = {s.device_id: s for s in rows}
    fleet = []
    for inv in inventory:
        dev = legacy.get(inv.logicmonitor_device_id) if inv.logicmonitor_device_id else None
        snap = snap_by_dev.get(dev.id) if dev else None
        site = sites.get(inv.site_id)
        criticality = inv.criticality or "Medium"
        # Physical chassis count: a switch stack is several physical switches behind one
        # logical device. Counted from the collected chassis evidence for ANY device type
        # (see inventory.switch_evidence) — an allowlist of {DSW, ASW} under-counted real
        # hardware, e.g. CA08-Z01-DAS-01 is a genuine 2-chassis C9300 stack. Types that do
        # not stack have no chassis evidence and fall back to 1.
        physical = (physical_switch_count(snap.details)[0] or 1) if snap else 1
        fleet.append({
            "device_id": dev.id if dev else None, "hostname": inv.generated_name, "lm_device_id": inv.logicmonitor_device_id,
            "match_status": (dev.match_status if dev else inv.logicmonitor_match_status), "monitored": dev is not None, "model": inv.model,
            "criticality": criticality, "band": _band(criticality), "physical": physical,
            "site_code": site.site_code if site else None,
            "city": (site.city if site else "Unassigned"),
            "province": (site.province_region if site else "") or "",
            "business_unit": (getattr(site, "business_unit", None) or "Unassigned"),
            "status": _health(snap) if dev else "Unknown", "snap": snap,
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
    monitored = sum(1 for d in fleet if d["monitored"])
    last_sync = max((d["collected_at"] for d in fleet if d["collected_at"]), default=None)
    fleet_ids = [d["device_id"] for d in fleet if d["device_id"]]
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
        "fleet_coverage": {"monitored": monitored, "total": len(fleet)},
        "lm_coverage": {"matched": matched, "total": len(fleet)},
        "sla_evidence_coverage": round(coverage, 1),
        "data_quality": quality,
    }


def global_sla(db: Session, fleet: list[dict]) -> dict:
    settings = get_settings()
    ids = [d["device_id"] for d in fleet if d["device_id"]]
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
    bands = {b: 0 for b in BANDS}
    degraded = unreachable = 0
    for d in fleet:
        bands[d["band"]] = bands.get(d["band"], 0) + 1
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
    ids = [d["device_id"] for d in fleet if d["device_id"]]
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
    weight = BAND_WEIGHT
    ranked = []
    for d in fleet:
        sev, label, value, status = _device_problem(d)
        score = sev * 10 + weight.get(d["band"], 2)
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
        ids = [m["device_id"] for m in members if m["device_id"]]
        avail = _fleet_window(db, ids, *sla._window_bounds("ytd", ref))
        avail30 = _fleet_window(db, ids, *sla._window_bounds("rolling_30", ref))
        statuses = [m["status"] for m in members]
        incidents = sum(1 for s in statuses if s in ("Critical", "Unknown"))
        degraded = sum(1 for s in statuses if s == "Warning")
        worst = "CRITICAL" if incidents else ("DEGRADED" if degraded else ("HEALTHY" if avail["availability"] is not None else "INSUFFICIENT EVIDENCE"))
        first = members[0]
        rows.append({
            "site_code": first["site_code"], "city": first["city"], "province": first["province"],
            "business_unit": first["business_unit"], "devices": sum(m["physical"] for m in members),
            "availability_ytd": avail["availability"], "availability_30d": avail30["availability"],
            "coverage": avail["coverage"], "device_health": worst,
            "critical_devices": incidents, "degraded_devices": degraded,
            "criticality": max((m["band"] for m in members), key=lambda b: BAND_WEIGHT.get(b, 2)),
            "status": worst,
            "since": _since_label(avail, ref),
            "reason": _commission_reason(avail, ref),
        })
    return rows


def _month_label(iso: str | None) -> str | None:
    """'2026-06-04' -> 'Jun 2026'."""
    if not iso:
        return None
    try:
        d = date.fromisoformat(iso)
        return d.strftime("%b %Y")
    except ValueError:
        return None


def _since_label(agg: dict, ref: date) -> str | None:
    """Only label a 'since <month>' when the *entire* group's data starts after Jan 1
    (a wholly new site/unit). Mixed groups (some devices since Jan) rely on the reason
    note instead, so the headline figure isn't mistaken for a partial year."""
    first = agg.get("first_observed")
    if first and date.fromisoformat(first) > date(ref.year, 1, 1):
        return _month_label(first)
    return None


def _commission_reason(agg: dict, ref: date) -> str | None:
    """Human explanation of a partial-year / not-yet-published window, so the UI can show
    a reason instead of a bare 'Insufficient'. Returns None when the full year is covered."""
    year_start = date(ref.year, 1, 1)
    first = agg.get("first_observed")
    newest = agg.get("newest_observed")
    if agg.get("availability") is None:
        # Genuinely below the evidence threshold even after trimming pre-commissioning days.
        if first and date.fromisoformat(first) > year_start:
            return f"New — monitoring began {_month_label(first)}; baseline still building."
        return "Awaiting sufficient telemetry to publish a governed figure."
    # Published, but did the measured period start after Jan 1 (a mid-year project)?
    if newest and date.fromisoformat(newest) > year_start + timedelta(days=7):
        return f"YTD reflects each site's monitored period; newest commissioned {_month_label(newest)}."
    return None


def business_units(db: Session, fleet: list[dict]) -> list[dict]:
    """Roll the fleet up by business unit (e.g. Healthcare vs Dental) for the compact
    summary above the site table: YTD & 30-day availability (coverage-gated), physical
    device count, number of sites, incident count, and an overall status. Units are
    ordered largest-first so the biggest part of the estate leads."""
    ref = sla.today_local()
    groups: dict[str, list[dict]] = {}
    for d in fleet:
        groups.setdefault(d["business_unit"] or "Unassigned", []).append(d)
    rows = []
    for unit, members in groups.items():
        ids = [m["device_id"] for m in members if m["device_id"]]
        avail = _fleet_window(db, ids, *sla._window_bounds("ytd", ref))
        avail30 = _fleet_window(db, ids, *sla._window_bounds("rolling_30", ref))
        statuses = [m["status"] for m in members]
        incidents = sum(1 for s in statuses if s in ("Critical", "Unknown"))
        degraded = sum(1 for s in statuses if s == "Warning")
        status = "CRITICAL" if incidents else ("DEGRADED" if degraded else ("HEALTHY" if avail["availability"] is not None else "INSUFFICIENT EVIDENCE"))
        sites = {m["site_code"] or m["city"] for m in members}
        rows.append({
            "business_unit": unit,
            "devices": sum(m["physical"] for m in members),
            "sites": len(sites),
            "availability_ytd": avail["availability"], "availability_30d": avail30["availability"],
            "coverage": avail["coverage"], "incidents": incidents, "degraded": degraded,
            "status": status,
            "since": _since_label(avail, ref),
            "reason": _commission_reason(avail, ref),
        })
    return sorted(rows, key=lambda r: r["devices"], reverse=True)


def _numlike(v):
    try:
        n = float(v)
        return n if n == n else None
    except (TypeError, ValueError):
        return None


def throughput(db: Session, fleet: list[dict]) -> dict:
    """Aggregate live interface throughput across all monitored operational interfaces,
    estimated as speed x utilisation. This sums every up interface (access + uplink), so it
    is a gross aggregate, not a WAN-egress figure; that caveat is returned with the value."""
    total_bps = 0.0
    counted = 0
    oldest_contributing = None  # oldest snapshot that actually fed a counted interface, not the
                                 # fleet's freshest device — the two can differ by a full refresh
                                 # cycle since switch_refresh.py polls devices one at a time.
    for d in fleet:
        snap = d["snap"]
        rows = (snap.details or {}).get("tables", {}).get("Interfaces", []) if snap and isinstance(snap.details, dict) else []
        contributed = False
        for r in rows or []:
            if str(r.get("Status")) != "up":
                continue
            speed = _numlike(r.get("Speed"))
            # Count both directions: total bits moved on the link = in + out (bidirectional).
            in_out = (_numlike(r.get("In Utilization %")) or 0.0) + (_numlike(r.get("Out Utilization %")) or 0.0)
            if speed and speed > 0:
                total_bps += speed * in_out / 100.0
                counted += 1
                contributed = True
        if contributed and snap and snap.collected_at:
            if oldest_contributing is None or snap.collected_at < oldest_contributing:
                oldest_contributing = snap.collected_at
    if counted == 0:
        return {"available": False, "reason": "No interface utilisation/speed collected yet.", "value": None, "unit": None}
    gbps = total_bps / 1e9
    value, unit = (round(gbps, 2), "Gbps") if gbps >= 1 else (round(total_bps / 1e6, 1), "Mbps")
    return {"available": True, "value": value, "unit": unit, "interfaces": counted,
            "label": "Snapshot utilisation (instant)",
            "oldest_contributing_sync": oldest_contributing.isoformat() if oldest_contributing else None,
            "note": ("Point-in-time in+out across all monitored up interfaces at the last poll — "
                     "not a busy-hour or average. See Path Resilience for the windowed peak/average.")}


def _iface_bps(details) -> float:
    """Aggregate in+out bits/sec across up interfaces of one snapshot (speed x utilisation)."""
    rows = (details or {}).get("tables", {}).get("Interfaces", []) if isinstance(details, dict) else []
    total = 0.0
    for r in rows or []:
        if str(r.get("Status")) != "up":
            continue
        sp = _numlike(r.get("Speed"))
        io = (_numlike(r.get("In Utilization %")) or 0.0) + (_numlike(r.get("Out Utilization %")) or 0.0)
        if sp and sp > 0:
            total += sp * io / 100.0
    return total


def _scale_bps(bps: float) -> dict:
    g = bps / 1e9
    return {"value": round(g, 2), "unit": "Gbps"} if g >= 1 else {"value": round(bps / 1e6, 1), "unit": "Mbps"}


def throughput_window(db: Session, hours: int = 24) -> dict:
    """Windowed fleet throughput (in+out) from stored snapshot history: hourly buckets over
    the last `hours`, each device carried forward to the next bucket, then average / peak / p95.
    Real average+busy-hour figure (unlike the instantaneous card), from data already collected."""
    hours = max(1, min(int(hours or 24), 24 * 30))
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    snaps = db.scalars(select(Snapshot).where(Snapshot.collected_at >= start).order_by(Snapshot.collected_at)).all()
    if not snaps:
        return {"available": False, "reason": "No snapshot history in the window yet.", "hours": hours}
    buckets: dict[datetime, dict[int, float]] = {}
    devices: set[int] = set()
    for s in snaps:
        hb = s.collected_at.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hb, {})[s.device_id] = _iface_bps(s.details)  # latest snapshot in the hour wins
        devices.add(s.device_id)
    last: dict[int, float] = {}
    series = []
    for hb in sorted(buckets):
        last.update(buckets[hb])
        fleet = sum(last.get(d, 0.0) for d in devices)
        series.append((hb, fleet))
    vals = [v for _, v in series]
    vals_sorted = sorted(vals)
    avg = sum(vals) / len(vals)
    peak = max(vals)
    p95 = vals_sorted[min(len(vals_sorted) - 1, int(len(vals_sorted) * 0.95))]
    return {
        "available": True, "hours": hours, "buckets": len(series),
        "average": _scale_bps(avg), "peak": _scale_bps(peak), "p95": _scale_bps(p95),
        "series": [{"t": hb.isoformat(), "gbps": round(v / 1e9, 3)} for hb, v in series],
        "note": ("In+out across all monitored up interfaces, hourly over the window (snapshots carried "
                 "forward). Fleet aggregate — WAN-egress-only split pending interface classification."),
    }


def executive_actions(db: Session, fleet: list[dict], gsla: dict) -> list[dict]:
    """Deterministic, business-impact-ranked recommendations. Each item names the affected
    asset, the evidence, and a concrete recommended next step — ordered so the highest
    business exposure (criticality x severity) surfaces first. Site-level clustering and a
    monitoring-coverage gap are included so leadership sees systemic issues, not just a
    list of individual devices."""
    band_w = BAND_WEIGHT
    actions = []
    # -- per-device conditions --
    for d in fleet:
        sev, label, value, status = _device_problem(d)
        b = d["band"]
        if status == "UNKNOWN" and b in ("Critical", "High"):
            actions.append({"severity": "P1", "title": f"Restore monitoring evidence for {d['hostname']}",
                            "detail": f"{d['hostname']} ({d['city']}, {b.lower()} criticality) has no recent LogicMonitor evidence, so its reliability can't be confirmed. "
                                      f"Recommended: verify the device is powered and reachable, then confirm the LogicMonitor collector and SNMP credentials.",
                            "device_id": d["device_id"], "_rank": 100 + band_w[b]})
        elif status == "CRITICAL":
            fix = "Dispatch to replace the failed hardware component" if label == "Hardware" else "Engage the on-call network engineer to triage the fault"
            actions.append({"severity": "P1", "title": f"Investigate critical condition on {d['hostname']}",
                            "detail": f"{d['hostname']} ({d['city']}) reports {label.lower()} {value}. Recommended: {fix} before it affects site availability.",
                            "device_id": d["device_id"], "_rank": 90 + band_w[b]})
        elif status in ("HIGH CPU", "HIGH MEM", "HIGH TEMP"):
            fix = {"HIGH CPU": "review control-plane load / ACLs and plan a capacity upgrade",
                   "HIGH MEM": "check for memory leaks or oversized tables and schedule a maintenance reload",
                   "HIGH TEMP": "inspect cooling/airflow and ambient temperature at the site"}[status]
            actions.append({"severity": "P2", "title": f"Address capacity risk on {d['hostname']}",
                            "detail": f"{d['hostname']} ({d['city']}) {label} at {value}, approaching its warning threshold. Recommended: {fix}.",
                            "device_id": d["device_id"], "_rank": 50 + band_w[b]})
    # -- site-level clustering: a site with several impaired devices is a systemic risk --
    site_hits: dict[str, dict] = {}
    for d in fleet:
        sev, _l, _v, status = _device_problem(d)
        if status in ("CRITICAL", "UNKNOWN", "HIGH CPU", "HIGH MEM", "HIGH TEMP", "WARNING"):
            s = site_hits.setdefault(d["city"], {"n": 0, "crit": False})
            s["n"] += 1
            s["crit"] = s["crit"] or status in ("CRITICAL", "UNKNOWN")
    for city, info in site_hits.items():
        if info["n"] >= 2:
            actions.append({"severity": "P1" if info["crit"] else "P2", "title": f"{city} site shows {info['n']} impaired devices",
                            "detail": f"{info['n']} devices at {city} are simultaneously degraded or unreachable — a possible shared cause (power, WAN, or upstream). "
                                      f"Recommended: check the site's power/WAN uplink and treat as a single site incident rather than isolated devices.",
                            "device_id": None, "_rank": 80 if info["crit"] else 60})
    # -- single-point-of-failure exposure (from the resilience model) --
    latest = resilience.latest_assessment(db)
    for dev in (latest.get("devices") or []):
        if dev.get("tier") == "Tier I" and dev.get("score", 0) <= 1:
            actions.append({"severity": "P2", "title": f"Reduce single-point-of-failure exposure at {dev['hostname']}",
                            "detail": f"{dev['hostname']} has no redundant uplink/stack/power path (Tier I-equivalent): healthy today, but one failure removes the site path. "
                                      f"Recommended: add a redundant uplink or stack member where the site's criticality justifies it.",
                            "device_id": dev.get("device_id"), "_rank": 40})
    # -- monitoring coverage gap --
    unmatched = sum(1 for d in fleet if d["match_status"] != "Matched")
    if unmatched:
        actions.append({"severity": "P3", "title": f"Map {unmatched} device(s) to LogicMonitor",
                        "detail": f"{unmatched} fleet device(s) are not yet matched to a LogicMonitor device, so they contribute no reliability evidence. "
                                  f"Recommended: complete the mapping in Device Fleet so they count toward the SLA.",
                        "device_id": None, "_rank": 30})
    # -- fleet SLA below target --
    if gsla["current"] is not None and gsla["current"] < gsla["target"]:
        actions.append({"severity": "P2", "title": "Fleet SLA below target",
                        "detail": f"30-day network availability is {sla.fmt_pct(gsla['current'])} against a {gsla['target']}% target. "
                                  f"Recommended: start with the lowest-availability sites in the table below and address their recurring downtime.",
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
    parts = [f"Canada network availability {verb} at {sla.fmt_pct(gsla['current'])} over the trailing 30 days against a {gsla['target']}% objective."]
    if crit["unreachable"]:
        parts.append(f"{crit['unreachable']} of {total} devices currently lack confirmed reachability evidence and are excluded from healthy counts.")
    else:
        parts.append("All monitored devices are currently reporting.")
    if worst and worst["availability_ytd"] is not None and worst["availability_ytd"] < gsla["target"]:
        parts.append(f"The lowest-performing site is {worst['city']} at {sla.fmt_pct(worst['availability_ytd'])} YTD.")
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
        "business_units": business_units(db, fleet),
        "sites": sites,
        "resilience": {"fleet_tier": resil.get("fleet_tier"), "as_of": resil.get("as_of")},
        "summary": executive_summary(db, fleet, gsla, crit, sites, hdr),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
