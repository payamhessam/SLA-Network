"""Background and on-demand device refresh from LogicMonitor.

`refresh_switch` re-collects one mapped Fleet device and writes a fresh Snapshot plus
an audit event; `refresh_all_switches` walks every enabled, mapped device (all types:
DSW/ASW/RTR/DAS/INR/...) and is run on a timer by `switch_refresh_loop` every
`switch_collection_interval_minutes` (30 by default). Locking is PER-DEVICE, not one
global lock: the only reason to serialise two refreshes of the SAME device is to avoid a
create-if-missing race on its legacy Device row. A single global lock used to also
serialise every on-demand "open detail" refresh behind the entire fleet sweep — right
after a restart (the fleet loop now runs immediately, not sleep-first) a user's
instant-open background refresh could silently queue for tens of minutes waiting for an
unrelated device's turn, defeating the point of opening instantly.
"""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .collection import collect_logicmonitor_device
from .config import get_settings
from .db import AuditEvent, Device, InventoryDevice, InventoryDeviceType, SessionLocal, Snapshot
from .logicmonitor import LogicMonitorClient

logger = logging.getLogger(__name__)
_device_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_fleet_refresh_lock = asyncio.Lock()


def _lock_for(inventory_id: int) -> asyncio.Lock:
    return _device_locks[inventory_id]


async def refresh_switch(db: Session, row: InventoryDevice, actor: str) -> Device:
    """Refresh one local switch from LogicMonitor using read-only API calls."""
    if not row.logicmonitor_device_id:
        raise ValueError("Switch is not mapped to LogicMonitor")

    legacy = db.scalar(
        select(Device).where(
            or_(
                Device.lm_device_id == row.logicmonitor_device_id,
                func.lower(Device.hostname) == row.generated_name.lower(),
            )
        )
    )
    if not legacy:
        legacy = Device(
            hostname=row.generated_name,
            management_ip=row.management_ip,
            site=row.site.city,
            role=row.role,
            criticality=row.criticality,
            device_type="switch",
            model=row.model,
            active=row.enabled,
            lm_device_id=row.logicmonitor_device_id,
            match_status=row.logicmonitor_match_status,
            notes=row.notes,
        )
        db.add(legacy)
        db.flush()

    client = LogicMonitorClient()
    payload = await client.get(f"/santaba/rest/device/devices/{row.logicmonitor_device_id}")
    remote = client.body(payload)
    normalized = await collect_logicmonitor_device(client, legacy, remote)

    legacy.management_ip = row.management_ip or legacy.management_ip
    legacy.model = normalized.get("model") or row.model or legacy.model
    legacy.lm_device_id = row.logicmonitor_device_id
    legacy.match_status = "Matched"
    legacy.site = row.site.city
    legacy.role = row.role
    legacy.active = row.enabled
    row.model = legacy.model
    row.last_logicmonitor_sync = datetime.now(timezone.utc)
    row.logicmonitor_match_status = "Matched"
    db.add(
        Snapshot(
            device_id=legacy.id,
            status=normalized["status"],
            availability=normalized["availability"],
            cpu=normalized["cpu"],
            memory=normalized["memory"],
            temperature=normalized["temperature"],
            details=normalized["details"],
        )
    )
    db.add(
        AuditEvent(
            actor=actor,
            action="switch.refresh",
            target=f"inventory_device:{row.id}",
            details={"logicmonitor_device_id": row.logicmonitor_device_id, "result": "success"},
        )
    )
    return legacy


async def refresh_switch_locked(db: Session, row: InventoryDevice, actor: str) -> Device:
    async with _lock_for(row.id):
        return await refresh_switch(db, row, actor)


_bg_tasks: set = set()


async def _refresh_bg(inventory_id: int, actor: str) -> None:
    try:
        with SessionLocal() as db:
            row = db.get(InventoryDevice, inventory_id)
            if row and row.logicmonitor_device_id:
                await refresh_switch_locked(db, row, actor)
                db.commit()
    except Exception as exc:  # never surface a background failure to the user
        logger.warning("Background open-detail refresh failed for inventory %s (%s)", inventory_id, type(exc).__name__)


def schedule_refresh(inventory_id: int, actor: str) -> bool:
    """Fire-and-forget a LogicMonitor refresh on the event loop so the detail view can
    open instantly from the last snapshot. Returns False if no loop is running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    task = loop.create_task(_refresh_bg(inventory_id, actor))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return True


async def refresh_all_switches(actor: str = "background-collector") -> dict:
    """Refresh every enabled, LogicMonitor-mapped device in Device Fleet (all types:
    DSW, ASW, RTR, DAS, INR, ...), every 30 minutes in the background."""
    # A watchdog and the normal timer can both notice a stale cycle.  One fleet lock prevents
    # them from multiplying read-only LogicMonitor requests while retaining per-device locks
    # for detail refreshes.
    async with _fleet_refresh_lock:
      with SessionLocal() as db:
        rows = db.scalars(
            select(InventoryDevice)
            .join(InventoryDeviceType)
            .where(
                InventoryDevice.enabled.is_(True),
                InventoryDevice.logicmonitor_device_id.is_not(None),
            )
            .order_by(InventoryDevice.id)
        ).all()
        collected = 0
        failures = 0
        for row in rows:
            try:
                # Per-device lock: only blocks a concurrent on-demand refresh of THIS SAME
                # device, not the whole fleet sweep. A user opening a different device's
                # detail page is never queued behind this loop.
                async with _lock_for(row.id):
                    await refresh_switch(db, row, actor)
                db.commit()
                collected += 1
            except Exception as exc:
                db.rollback()
                failures += 1
                logger.warning("Background switch refresh failed for inventory id %s (%s)", row.id, type(exc).__name__)
        return {"requested": len(rows), "collected": collected, "failures": failures}


async def _oldest_snapshot_age_minutes() -> float | None:
    """Age of the oldest latest snapshot for an active, mapped device.

    None means there is no current snapshot set at all, which the watchdog treats as stale.
    This is a local DB-only check; it does not spend LogicMonitor API budget by itself.
    """
    with SessionLocal() as db:
        ids = db.scalars(select(Device.id).where(Device.active.is_(True), Device.lm_device_id.is_not(None))).all()
        if not ids:
            return 0.0
        latest = db.scalar(select(func.min(Snapshot.collected_at)).where(
            Snapshot.device_id.in_(ids),
            Snapshot.collected_at.in_(select(func.max(Snapshot.collected_at)).where(Snapshot.device_id.in_(ids)).group_by(Snapshot.device_id)),
        ))
        if not latest:
            return None
        return max(0.0, (datetime.now(timezone.utc) - latest).total_seconds() / 60)


async def switch_freshness_watchdog_loop() -> None:
    """Recover a stalled collector before a snapshot can be older than one hour."""
    while True:
        try:
            age = await _oldest_snapshot_age_minutes()
            # A normal sweep currently finishes within ~20 minutes. Start recovery at 40
            # minutes, leaving the sweep time before the published one-hour freshness limit.
            if (age is None or age > 40) and not _fleet_refresh_lock.locked():
                logger.warning("Snapshot freshness backstop starting refresh (age=%s min)", None if age is None else round(age, 1))
                result = await refresh_all_switches("freshness-watchdog")
                logger.info("Snapshot freshness backstop completed: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Snapshot freshness backstop failed (%s)", type(exc).__name__)
        await asyncio.sleep(300)


async def switch_refresh_loop() -> None:
    """Refresh every switch at startup, then every interval. Runs first (not sleep-first) so a
    container restart never leaves the fleet stale for up to a full interval."""
    interval_seconds = max(1, get_settings().switch_collection_interval_minutes) * 60
    while True:
        try:
            result = await refresh_all_switches()
            logger.info("Background switch refresh completed: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Background switch refresh run failed (%s)", type(exc).__name__)
        await asyncio.sleep(interval_seconds)
