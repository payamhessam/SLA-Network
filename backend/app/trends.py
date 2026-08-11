"""Trend intelligence — weekly availability vs target, week-over-week / month-over-month
deltas, availability-derived incidents, and approximate MTTR/MTBF. Fleet-scoped, read
from the sla_daily rollup (no live LogicMonitor). Everything is coverage-gated: a window
with insufficient evidence yields no score rather than a fabricated value.
"""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import overview, sla
from .config import get_settings
from .db import SlaDaily


def _fleet_ids(db: Session) -> list[int]:
    return [d["device_id"] for d in overview._fleet(db) if d["device_id"]]


def _ids(db, fleet):
    return [d["device_id"] for d in (fleet if fleet is not None else overview._fleet(db)) if d["device_id"]]


def availability_trend(db: Session, weeks: int = 12, fleet=None) -> dict:
    """Weekly fleet availability against the SLA target, for the compliance-trend chart."""
    target = get_settings().sla_target
    ids = _ids(db, fleet)
    ref = sla.today_local()
    this_monday = ref - timedelta(days=ref.weekday())  # weeks start Monday, local tz
    series = []
    for w in range(weeks - 1, -1, -1):
        start = this_monday - timedelta(weeks=w)
        end = min(start + timedelta(days=6), ref)
        agg = overview._fleet_window(db, ids, start, end)
        series.append({
            "week_start": start.isoformat(),
            "availability": agg["availability"], "coverage": agg["coverage"], "status": agg["status"],
            "below_target": agg["availability"] is not None and agg["availability"] < target,
            "downtime_minutes": max(0, agg["observed_minutes"] - agg["up_minutes"]),
            "observed_minutes": agg["observed_minutes"],
        })
    return {"target": target, "weeks": weeks, "series": series}


def _trend_label(delta):
    if delta is None:
        return "UNKNOWN"
    if delta > 0.001:
        return "IMPROVING"
    if delta < -0.001:
        return "WORSENING"
    return "UNCHANGED"


def deltas(db: Session, fleet=None) -> dict:
    """Week-over-week and month-over-month availability change for the fleet."""
    ids = _ids(db, fleet)
    ref = sla.today_local()

    def win(start, end):
        return overview._fleet_window(db, ids, start, end)["availability"]

    wow_cur = win(ref - timedelta(days=ref.weekday()), ref)
    wow_prev = win(ref - timedelta(days=ref.weekday() + 7), ref - timedelta(days=ref.weekday() + 1))
    mom_cur = win(ref - timedelta(days=29), ref)
    mom_prev = win(ref - timedelta(days=59), ref - timedelta(days=30))

    def diff(a, b):
        return round(a - b, 4) if (a is not None and b is not None) else None

    return {
        "wow": {"current": wow_cur, "previous": wow_prev, "delta": diff(wow_cur, wow_prev), "trend": _trend_label(diff(wow_cur, wow_prev))},
        "mom": {"current": mom_cur, "previous": mom_prev, "delta": diff(mom_cur, mom_prev), "trend": _trend_label(diff(mom_cur, mom_prev))},
    }


def incidents(db: Session, days: int = 90, fleet=None) -> list[dict]:
    """Derive incidents from contiguous below-100%-availability days per device (coverage-gated).

    Note: this is availability-derived, not a discrete event log — LogicMonitor alert history
    would give exact start/stop timestamps. Duration is the summed down-minutes across the run.
    """
    settings = get_settings()
    ref = sla.today_local()
    start = ref - timedelta(days=days - 1)
    fleet = fleet if fleet is not None else overview._fleet(db)
    ids = [d["device_id"] for d in fleet if d["device_id"]]
    by_dev: dict[int, list[SlaDaily]] = {}
    if ids:  # one query for the whole window, then group per device
        for r in db.scalars(select(SlaDaily).where(SlaDaily.device_id.in_(ids), SlaDaily.day >= start, SlaDaily.day <= ref).order_by(SlaDaily.day)).all():
            by_dev.setdefault(r.device_id, []).append(r)
    out = []
    for d in fleet:
        if not d["device_id"]:
            continue
        rows = by_dev.get(d["device_id"], [])
        run = None
        for r in rows:
            bad = r.coverage >= settings.coverage_threshold and r.availability is not None and r.availability < 100.0
            if bad:
                down = max(0, r.observed_minutes - r.up_minutes)
                run = {"start": r.day, "end": r.day, "down": down} if run is None else {**run, "end": r.day, "down": run["down"] + down}
            elif run:
                out.append({**run, "device": d["hostname"], "city": d["city"], "device_id": d["device_id"]}); run = None
        if run:
            out.append({**run, "device": d["hostname"], "city": d["city"], "device_id": d["device_id"]})
    for i in out:
        i["days"] = (i["end"] - i["start"]).days + 1
        i["start"], i["end"] = i["start"].isoformat(), i["end"].isoformat()
    out.sort(key=lambda x: -x["down"])
    return out


def mttr_mtbf(db: Session, days: int = 90, inc=None, fleet=None) -> dict:
    """Approximate fleet MTTR/MTBF from availability-derived incidents (labelled as derived)."""
    fleet = fleet if fleet is not None else overview._fleet(db)
    inc = inc if inc is not None else incidents(db, days, fleet)
    total_down = sum(i["down"] for i in inc)
    n = len(inc)
    fleet_n = len(fleet)
    period_min = days * 24 * 60
    return {
        "incidents": n, "window_days": days, "total_downtime_minutes": total_down,
        "mttr_minutes": round(total_down / n) if n else None,
        "mtbf_hours": round((period_min * fleet_n / n) / 60) if n else None,
        "basis": "Derived from availability history (coverage-gated); exact timings require LM alert history.",
    }


def build(db: Session) -> dict:
    fleet = overview._fleet(db)  # compute the fleet once and thread it through
    inc = incidents(db, fleet=fleet)
    shown = inc[:25]
    return {
        "availability_trend": availability_trend(db, fleet=fleet),
        "deltas": deltas(db, fleet=fleet),
        # `incidents` is the most-recent slice for display only. The count is stated
        # explicitly so a consumer can never mistake len(incidents) for the real total —
        # the authoritative count (used by the Excel/PPTX "Incidents (90 days)" KPI) is
        # mttr_mtbf.incidents.
        "incidents": shown,
        "incidents_shown": len(shown),
        "incidents_total": len(inc),
        "incidents_truncated": len(inc) > len(shown),
        "mttr_mtbf": mttr_mtbf(db, inc=inc, fleet=fleet),
    }
