"""SLA history: pull availability from LogicMonitor, persist a daily rollup, and
derive WTD/MTD/QTD/rolling-30/YTD windows from the stored rows.

Availability model (matches docs/assessment-and-design.md):
  a sample is "up" when PingLossPercent < 100 (device answered at least one probe).
  availability = up_minutes / observed_minutes
  coverage     = observed_minutes / expected_minutes
Windows below the coverage threshold report "Insufficient evidence"; missing
evidence is never fabricated as 0 or 100.
"""
import asyncio
import logging
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import Device, SessionLocal, SlaDaily
from .logicmonitor import LogicMonitorClient

logger = logging.getLogger(__name__)

LOSS_DATAPOINT = "PingLossPercent"
COVERAGE_MIN_SAMPLES = 2
CONCURRENCY = 16  # global cap on in-flight LM requests during a backfill
DEVICE_CONCURRENCY = 6  # cap on devices processed at once (each holds one DB session)
_backfill_lock = asyncio.Lock()


def tz() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().sla_timezone)
    except Exception:
        return ZoneInfo("UTC")


def today_local() -> date:
    return datetime.now(tz()).date()


def backfill_start() -> date:
    try:
        return date.fromisoformat(get_settings().sla_backfill_start)
    except ValueError:
        return date(today_local().year, 1, 1)


def day_bounds_epoch(day: date) -> tuple[int, int]:
    """Local-midnight to next local-midnight for `day`, as UTC epoch seconds."""
    zone = tz()
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def _interval_seconds(times: list) -> float:
    """Median sample interval. LM returns `time` newest-first, so take abs()."""
    numeric = [t for t in times if isinstance(t, (int, float))]
    if len(numeric) < 2:
        return 120.0
    diffs = sorted(abs(numeric[i + 1] - numeric[i]) for i in range(len(numeric) - 1))
    mid = diffs[len(diffs) // 2] / 1000.0
    return mid if mid > 0 else 120.0


def _counts_from_samples(data: dict) -> tuple[float, float, bool]:
    """(observed_minutes, up_minutes, has_datapoint) for one instance-data window."""
    points = data.get("dataPoints", []) or []
    values = data.get("values", []) or []
    times = data.get("time", []) or []
    if LOSS_DATAPOINT not in points:
        return 0.0, 0.0, False
    idx = points.index(LOSS_DATAPOINT)
    interval = _interval_seconds(times)
    observed = up = 0
    for row in values:
        if idx >= len(row):
            continue
        try:
            loss = float(row[idx])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(loss):
            continue
        observed += 1
        if loss < 100:
            up += 1
    return observed * interval / 60.0, up * interval / 60.0, True


async def ping_instance(client: LogicMonitorClient, lm_device_id: int):
    applied = await client.applied_datasources(lm_device_id)
    name = get_settings().availability_source
    ds = next((x for x in applied if (x.get("dataSourceName") or x.get("name")) == name), None)
    if not ds:
        return None
    instances = await client.instances(lm_device_id, int(ds["id"]))
    if not instances:
        return None
    return int(ds["id"]), int(instances[0]["id"])


def _upsert(db: Session, device_id: int, day: date, metrics: dict, source: str) -> None:
    row = db.scalar(select(SlaDaily).where(SlaDaily.device_id == device_id, SlaDaily.day == day))
    if not row:
        row = SlaDaily(device_id=device_id, day=day)
        db.add(row)
    row.expected_minutes = metrics["expected_minutes"]
    row.observed_minutes = metrics["observed_minutes"]
    row.up_minutes = metrics["up_minutes"]
    row.availability = metrics["availability"]
    row.coverage = metrics["coverage"]
    row.source = source


async def backfill_device(device: Device, start: date, end: date, force: bool = False, sem: asyncio.Semaphore | None = None) -> dict:
    """Fill SlaDaily for [start, end] (inclusive) for one mapped device, using its own DB session.
    `sem` (shared across devices) caps total in-flight LM requests."""
    if not device.lm_device_id:
        return {"device": device.hostname, "status": "unmapped", "written": 0}
    sem = sem or asyncio.Semaphore(12)
    client = LogicMonitorClient()
    discovery = await ping_instance(client, device.lm_device_id)
    if not discovery:
        return {"device": device.hostname, "status": "no ping datasource", "written": 0}
    hds, inst = discovery
    settings = get_settings()
    threshold = settings.coverage_threshold
    chunk = max(1, settings.sla_query_hours) * 3600  # keep each request under LM's per-call point cap

    async def day_metrics(day: date):
        s0, e0 = day_bounds_epoch(day)
        e0 = min(e0, int(datetime.now(timezone.utc).timestamp()))  # today is partial: expected = minutes elapsed, not a full day
        windows = []
        a = s0
        while a < e0:
            b = min(a + chunk, e0)
            windows.append((a, b))
            a = b

        async def one(a: int, b: int):
            async with sem:
                try:
                    return _counts_from_samples(await client.instance_data(device.lm_device_id, hds, inst, a, b))
                except Exception:
                    return 0.0, 0.0, False

        parts = await asyncio.gather(*(one(a, b) for a, b in windows))
        observed = round(sum(p[0] for p in parts))
        up_minutes = round(sum(p[1] for p in parts))
        expected = round((e0 - s0) / 60)
        availability = (100.0 * up_minutes / observed) if observed else None
        coverage = min(100.0, (100.0 * observed / expected)) if expected else 0.0
        return day, {"expected_minutes": expected, "observed_minutes": observed, "up_minutes": up_minutes, "availability": availability, "coverage": coverage}

    written = 0
    with SessionLocal() as db:
        existing = {r.day: r for r in db.scalars(select(SlaDaily).where(SlaDaily.device_id == device.id, SlaDaily.day >= start, SlaDaily.day <= end)).all()}
        days = [start + timedelta(n) for n in range((end - start).days + 1)]
        pending = [d for d in days if force or not (existing.get(d) and existing[d].coverage >= threshold)]
        pending.sort(reverse=True)  # recent-first so WTD/MTD windows become valid quickly
        for i in range(0, len(pending), 12):  # commit every ~12 days so progress is visible incrementally
            for day, metrics in await asyncio.gather(*(day_metrics(d) for d in pending[i:i + 12])):
                _upsert(db, device.id, day, metrics, settings.availability_source)
                written += 1
            db.commit()
    return {"device": device.hostname, "status": "ok", "written": written, "days": len(days)}


def monitored_devices(db: Session):
    return db.scalars(select(Device).where(Device.lm_device_id.is_not(None), Device.active.is_(True)).order_by(Device.hostname)).all()


async def backfill_all(start: date | None = None, end: date | None = None, force: bool = False) -> dict:
    """One-time (or admin-triggered) backfill for every mapped switch."""
    async with _backfill_lock:
        start = start or backfill_start()
        end = end or today_local()
        recent_start = max(start, end - timedelta(days=35))
        sem = asyncio.Semaphore(CONCURRENCY)
        device_sem = asyncio.Semaphore(DEVICE_CONCURRENCY)
        with SessionLocal() as db:
            devices = monitored_devices(db)
            [(d.id, d.lm_device_id, d.hostname, d.device_type) for d in devices]  # force-load before the session closes

        async def run(device, window_start):
            async with device_sem:  # bound concurrent DB sessions so the pool is not exhausted
                return await backfill_device(device, window_start, end, force, sem)

        # Pass 1: recent weeks for every device, so WTD/MTD light up fleet-wide within a minute or two.
        if recent_start > start:
            await asyncio.gather(*(run(d, recent_start) for d in devices), return_exceptions=True)
        # Pass 2: full history back to the start (fills YTD); recent good days are skipped when force is False.
        outcomes = await asyncio.gather(*(run(d, start) for d in devices), return_exceptions=True)
        results = []
        for device, outcome in zip(devices, outcomes):
            if isinstance(outcome, dict):
                results.append(outcome)
            else:
                logger.warning("SLA backfill failed for %s (%s)", device.hostname, type(outcome).__name__)
                results.append({"device": device.hostname, "status": f"error:{type(outcome).__name__}", "written": 0})
        return {"start": start.isoformat(), "end": end.isoformat(), "devices": len(results), "results": results}


async def refresh_recent(days_back: int = 1) -> dict:
    """Recompute yesterday..today for every mapped switch (idempotent, forward fill)."""
    end = today_local()
    start = end - timedelta(days=days_back)
    return await backfill_all(start, end, force=True)


# ---- window aggregation (read-only, from the stored rollup) ----

def _window_bounds(kind: str, ref: date) -> tuple[date, date]:
    if kind == "wtd":
        return ref - timedelta(days=ref.weekday()), ref
    if kind == "mtd":
        return ref.replace(day=1), ref
    if kind == "qtd":
        return date(ref.year, ((ref.month - 1) // 3) * 3 + 1, 1), ref
    if kind == "rolling_30":
        return ref - timedelta(days=29), ref
    if kind == "ytd":
        return date(ref.year, 1, 1), ref
    raise ValueError(kind)


def _aggregate(rows: list[SlaDaily]) -> dict:
    """Aggregate daily rollup rows into availability + coverage.

    Coverage is measured over each device's *observable* period, not the raw calendar
    window: for every device we drop the leading run of days with zero observed minutes
    (the period before it was commissioned / added to monitoring). Measuring availability
    before a device was ever polled is meaningless and would unfairly show a mid-year
    project as "Insufficient". Genuine outages still count — a device that is down is
    still *observed* (ping runs, loss=100%), so only truly un-monitored lead-in days are
    trimmed. The commissioning dates are returned so a partial-year figure can be labelled
    honestly instead of blanked.
    """
    threshold = get_settings().coverage_threshold
    by_device: dict[int | None, list[SlaDaily]] = {}
    for r in rows:
        by_device.setdefault(getattr(r, "device_id", None), []).append(r)
    expected = observed = up = 0
    first_days: list[date] = []
    for drows in by_device.values():
        if all(getattr(r, "day", None) is not None for r in drows):
            drows.sort(key=lambda r: r.day)
        start = next((i for i, r in enumerate(drows) if r.observed_minutes > 0), None)
        if start is None:
            continue  # this device was never observed in the window
        measurable = drows[start:]
        day0 = getattr(measurable[0], "day", None)
        if day0 is not None:
            first_days.append(day0)
        expected += sum(r.expected_minutes for r in measurable)
        observed += sum(r.observed_minutes for r in measurable)
        up += sum(r.up_minutes for r in measurable)
    coverage = (100.0 * observed / expected) if expected else 0.0
    availability = (100.0 * up / observed) if observed else None
    sufficient = coverage >= threshold and availability is not None
    return {
        "availability": round(availability, 4) if sufficient else None,
        "coverage": round(coverage, 2),
        "up_minutes": up,
        "observed_minutes": observed,
        "expected_minutes": expected,
        "days": len(rows),
        "first_observed": min(first_days).isoformat() if first_days else None,
        "newest_observed": max(first_days).isoformat() if first_days else None,
        "status": "ok" if sufficient else "Insufficient evidence",
    }


def window(db: Session, device_id: int, start: date, end: date) -> dict:
    rows = db.scalars(select(SlaDaily).where(SlaDaily.device_id == device_id, SlaDaily.day >= start, SlaDaily.day <= end)).all()
    result = _aggregate(rows)
    result["start"] = start.isoformat()
    result["end"] = end.isoformat()
    return result


def device_sla(db: Session, device_id: int) -> dict:
    ref = today_local()
    out = {}
    for kind in ("wtd", "mtd", "qtd", "rolling_30", "ytd"):
        start, end = _window_bounds(kind, ref)
        out[kind] = window(db, device_id, start, end)
    return out


def fleet_sla(db: Session) -> dict:
    ref = today_local()
    devices = monitored_devices(db)
    target = get_settings().sla_target
    entries = []
    ytd_rows: list[SlaDaily] = []
    wtd_rows: list[SlaDaily] = []
    ys, ye = _window_bounds("ytd", ref)
    ws, we = _window_bounds("wtd", ref)
    below = 0
    # Load every device's YTD rows (WTD is a subset) in ONE query, then window in memory.
    dev_ids = [d.id for d in devices]
    by_dev: dict[int, list[SlaDaily]] = {}
    if dev_ids:
        for r in db.scalars(select(SlaDaily).where(SlaDaily.device_id.in_(dev_ids), SlaDaily.day >= ys, SlaDaily.day <= ye)).all():
            by_dev.setdefault(r.device_id, []).append(r)

    def _win(rows, start, end):
        agg = _aggregate([r for r in rows if start <= r.day <= end])
        agg["start"] = start.isoformat(); agg["end"] = end.isoformat()
        return agg

    for d in devices:
        drows = by_dev.get(d.id, [])
        dy = _win(drows, ys, ye)
        dw = _win(drows, ws, we)
        entries.append({"device_id": d.id, "lm_device_id": d.lm_device_id, "hostname": d.hostname, "site": d.site, "wtd": dw, "ytd": dy})
        ytd_rows += drows
        wtd_rows += [r for r in drows if ws <= r.day <= we]
        if dy["availability"] is not None and dy["availability"] < target:
            below += 1
    fleet_ytd = _aggregate(ytd_rows)
    fleet_wtd = _aggregate(wtd_rows)
    measured = [e for e in entries if e["ytd"]["availability"] is not None]
    return {
        "target": target,
        "coverage_threshold": get_settings().coverage_threshold,
        "timezone": get_settings().sla_timezone,
        "as_of": ref.isoformat(),
        "backfill_start": backfill_start().isoformat(),
        "fleet_ytd": fleet_ytd,
        "fleet_wtd": fleet_wtd,
        "below_target": below,
        "monitored": len(devices),
        "measured": len(measured),
        "devices": entries,
    }


async def has_history() -> bool:
    with SessionLocal() as db:
        return db.scalar(select(SlaDaily.id).limit(1)) is not None


async def needs_backfill() -> bool:
    """True when any mapped, active device has no SLA history yet — e.g. a device added
    after the initial backfill. Lets startup top-up newcomers instead of only seeding on
    a completely empty database."""
    with SessionLocal() as db:
        have = {r[0] for r in db.execute(select(SlaDaily.device_id).distinct()).all()}
        return any(d.id not in have for d in monitored_devices(db))


async def sla_rollup_loop() -> None:
    """Every 6h: recompute yesterday..today for every mapped switch."""
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            result = await refresh_recent(1)
            logger.info("SLA rollup completed for %s devices", result["devices"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("SLA rollup failed (%s)", type(exc).__name__)
