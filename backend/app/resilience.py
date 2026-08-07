"""Network-resilience estimate mapped onto the Uptime Institute Tier framework.

IMPORTANT: Uptime Institute Tiers certify *facility* power/cooling paths and are
not a network metric. This module produces a network-layer resilience ESTIMATE
from LogicMonitor-observable signals (redundant uplinks, stack members, dual
power, measured availability) mapped onto the same I-IV bands. It is not a
certification.
"""
import asyncio
import logging
import math
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import sla
from .config import get_settings
from .db import Device, ResilienceAssessment, SessionLocal, Snapshot
from .inventory import physical_switch_count

logger = logging.getLogger(__name__)

TIER_SCORE = {"Tier I": 1, "Tier II": 2, "Tier III": 3, "Tier IV": 4, "Insufficient": 0}
TIERS = [
    {"tier": "Tier I", "availability": 99.671, "max_annual_downtime": "28.8 hours", "summary": "Single path, no redundancy"},
    {"tier": "Tier II", "availability": 99.741, "max_annual_downtime": "22.0 hours", "summary": "Redundant power/cooling components (N+1)"},
    {"tier": "Tier III", "availability": 99.982, "max_annual_downtime": "1.6 hours", "summary": "Concurrently maintainable, dual paths"},
    {"tier": "Tier IV", "availability": 99.995, "max_annual_downtime": "26.3 minutes", "summary": "Fault tolerant, 2N / 2N+1"},
]
_UPLINK = re.compile(r"dsw|asw|csw|dist|core|rtr|router|switch", re.I)
_AP = re.compile(r"wap|(?:^|[-_])ap(?:[-_]|$)", re.I)


def _num(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def device_signals(details: dict) -> dict:
    tables = (details or {}).get("tables", {}) if isinstance(details, dict) else {}
    count, count_source = physical_switch_count(details)
    stack = count if count else 1
    env = tables.get("Environmental and PoE", []) or []
    psu_ok = [r for r in env if str(r.get("Category", "")) == "Power Supply" and str(r.get("State", "")) in ("Normal", "normal")]
    uplinks = set()
    for r in tables.get("CDP-LLDP Neighbors", []) or []:
        name = str(r.get("Neighbor") or "")
        if name and _UPLINK.search(name) and not _AP.search(name):
            uplinks.add(name.lower())
    speeds = [s for s in (_num(r.get("Speed")) for r in tables.get("Interfaces", []) or []) if s]
    return {
        "stack_members": stack,
        "stack_known": count is not None,
        "stack_source": count_source,
        "dual_power": len(psu_ok) >= 2,
        "power_supplies": len(psu_ok),
        "redundant_uplinks": len(uplinks),
        "max_link_bps": max(speeds) if speeds else None,
    }


def tier_of(signals: dict, availability_ytd: float | None) -> tuple[str, list[str]]:
    stack = signals["stack_members"]
    dual_power = signals["dual_power"]
    uplinks = signals["redundant_uplinks"]
    n_plus_1 = dual_power or stack >= 2
    two_paths = uplinks >= 2
    reasons = []
    reasons.append(f"{uplinks} redundant upstream path(s)")
    reasons.append(f"{stack} stack member(s)" + ("" if signals["stack_known"] else " (unconfirmed)"))
    reasons.append(f"{signals['power_supplies']} healthy power supply(ies)")
    if availability_ytd is not None:
        reasons.append(f"measured YTD availability {availability_ytd:.3f}%")
    if two_paths and stack >= 2 and dual_power and availability_ytd is not None and availability_ytd >= 99.995:
        tier = "Tier IV"
        reasons.append("Dual paths + redundant supervisors + dual power + measured fault tolerance -> fault tolerant")
    elif two_paths and n_plus_1:
        tier = "Tier III"
        reasons.append("Second independent path plus N+1 components -> concurrently maintainable")
    elif n_plus_1:
        tier = "Tier II"
        reasons.append("N+1 components but a single distribution path")
    elif uplinks >= 1 or signals["stack_known"]:
        tier = "Tier I"
        reasons.append("Single path with no component redundancy")
    else:
        tier = "Insufficient"
        reasons.append("No collected redundancy evidence yet")
    return tier, reasons


def assess_device(db: Session, device: Device) -> dict:
    snap = db.scalar(select(Snapshot).where(Snapshot.device_id == device.id).order_by(Snapshot.collected_at.desc()).limit(1))
    details = snap.details if snap and snap.details else {}
    signals = device_signals(details)
    wtd = sla.window(db, device.id, *sla._window_bounds("wtd", sla.today_local()))
    ytd = sla.window(db, device.id, *sla._window_bounds("ytd", sla.today_local()))
    tier, reasons = tier_of(signals, ytd["availability"])
    return {
        "device_id": device.id,
        "hostname": device.hostname,
        "site": device.site,
        "device_type": device.device_type,
        "tier": tier,
        "score": TIER_SCORE[tier],
        "signals": signals,
        "rationale": reasons,
        "availability_wtd": wtd["availability"],
        "availability_ytd": ytd["availability"],
    }


def assess_fleet(db: Session) -> dict:
    devices = [d for d in sla.monitored_devices(db) if d.device_type != "access_point"]
    per_device = [assess_device(db, d) for d in devices]
    evaluated = [a for a in per_device if a["score"] > 0]
    fleet = sla.fleet_sla(db)
    if evaluated:
        weakest = min(evaluated, key=lambda a: a["score"])
        fleet_tier = weakest["tier"]
        rationale = [
            f"Fleet resilience is limited by its weakest critical node: {weakest['hostname']} ({weakest['tier']}).",
            f"{sum(a['score'] >= 3 for a in evaluated)} of {len(evaluated)} switches meet Tier III-equivalent; "
            f"{sum(a['score'] == 4 for a in evaluated)} reach Tier IV-equivalent.",
            "Estimate reflects network-layer redundancy only, not a facility (power/cooling) certification.",
        ]
    else:
        fleet_tier = "Insufficient"
        rationale = ["No collected redundancy evidence yet; run collection and let the SLA rollup accumulate."]
    now = datetime.now(timezone.utc)
    db.add(ResilienceAssessment(created_at=now, scope="fleet", device_id=None, tier=fleet_tier, score=TIER_SCORE[fleet_tier],
                                availability_wtd=fleet["fleet_wtd"]["availability"], availability_ytd=fleet["fleet_ytd"]["availability"],
                                signals={"evaluated": len(evaluated), "monitored": len(devices)}, rationale=rationale))
    for a in per_device:
        db.add(ResilienceAssessment(created_at=now, scope="device", device_id=a["device_id"], tier=a["tier"], score=a["score"],
                                    availability_wtd=a["availability_wtd"], availability_ytd=a["availability_ytd"],
                                    signals=a["signals"], rationale=a["rationale"]))
    db.commit()
    return {"as_of": now.isoformat(), "fleet_tier": fleet_tier, "fleet_score": TIER_SCORE[fleet_tier], "rationale": rationale, "tiers": TIERS, "devices": per_device}


async def run_assessment() -> dict:
    with SessionLocal() as db:
        return assess_fleet(db)


def latest_assessment(db: Session) -> dict:
    fleet = db.scalar(select(ResilienceAssessment).where(ResilienceAssessment.scope == "fleet").order_by(ResilienceAssessment.created_at.desc()).limit(1))
    if not fleet:
        return {"as_of": None, "fleet_tier": "Insufficient", "fleet_score": 0, "rationale": ["Assessment has not run yet."], "tiers": TIERS, "devices": []}
    device_rows = db.scalars(select(ResilienceAssessment).where(ResilienceAssessment.scope == "device", ResilienceAssessment.created_at == fleet.created_at)).all()
    hosts = {d.id: d.hostname for d in db.scalars(select(Device)).all()}
    devices = [{"device_id": r.device_id, "hostname": hosts.get(r.device_id, str(r.device_id)), "tier": r.tier, "score": r.score,
                "signals": r.signals, "rationale": r.rationale, "availability_wtd": r.availability_wtd, "availability_ytd": r.availability_ytd}
               for r in sorted(device_rows, key=lambda r: r.score)]
    return {"as_of": fleet.created_at.isoformat(), "fleet_tier": fleet.tier, "fleet_score": fleet.score,
            "availability_wtd": fleet.availability_wtd, "availability_ytd": fleet.availability_ytd,
            "rationale": fleet.rationale, "tiers": TIERS, "devices": devices}


async def resilience_loop() -> None:
    """Run the network-resilience assessment at startup and every 12 hours."""
    while True:
        try:
            await run_assessment()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Resilience assessment failed (%s)", type(exc).__name__)
        await asyncio.sleep(12 * 3600)
