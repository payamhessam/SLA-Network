"""Background and on-demand device refresh from LogicMonitor.

`refresh_switch` re-collects one mapped Fleet device and writes a fresh Snapshot plus
an audit event; `refresh_all_switches` walks every enabled, mapped device (all types:
DSW/ASW/RTR/DAS/INR/...) and is run on a timer by `switch_refresh_loop` every
`switch_collection_interval_minutes` (30 by default). A module-level asyncio lock
serialises refreshes so a manual refresh and the background loop can't collide.
"""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .collection import collect_logicmonitor_device
from .config import get_settings
from .db import AuditEvent, Device, InventoryDevice, InventoryDeviceType, SessionLocal, Snapshot
from .logicmonitor import LogicMonitorClient

logger = logging.getLogger(__name__)
_refresh_lock = asyncio.Lock()


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
    async with _refresh_lock:
        return await refresh_switch(db, row, actor)


async def refresh_all_switches() -> dict:
    """Refresh every enabled, LogicMonitor-mapped device in Device Fleet (all types:
    DSW, ASW, RTR, DAS, INR, ...), every 30 minutes in the background."""
    async with _refresh_lock:
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
                    await refresh_switch(db, row, "background-collector")
                    db.commit()
                    collected += 1
                except Exception as exc:
                    db.rollback()
                    failures += 1
                    logger.warning("Background switch refresh failed for inventory id %s (%s)", row.id, type(exc).__name__)
            return {"requested": len(rows), "collected": collected, "failures": failures}


async def switch_refresh_loop() -> None:
    interval_seconds = max(1, get_settings().switch_collection_interval_minutes) * 60
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            result = await refresh_all_switches()
            logger.info("Background switch refresh completed: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Background switch refresh run failed (%s)", type(exc).__name__)
