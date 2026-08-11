"""Failure-injection regression tests for the SLA rollup (2026-08-11 audit).

Locks in the most serious defect found by that audit: a LogicMonitor outage during the
scheduled 6-hourly rollup (sla_rollup_loop -> refresh_recent(1) -> backfill_all(force=True))
used to OVERWRITE known-good stored days with observed=0 / coverage=0, silently destroying
real measured SLA history and turning it into "Insufficient evidence".

Both directions are tested, because the fix must not break normal collection:
  * LM failing  -> existing evidence preserved, nothing fabricated
  * LM working  -> the day is written exactly as measured
"""
import asyncio
import os
import tempfile
from datetime import date, timedelta

os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'lmfail.db')}")
os.environ.setdefault("LOCAL_ADMIN_PASSWORD", "correct-horse-battery-staple")
os.environ.setdefault("LOCAL_USER_PASSWORD", "viewer-password-long")
os.environ.setdefault("JWT_SECRET", "test-secret-long-enough-abcdefgh")

import pytest

from app import sla
from app.db import Base, Device, SessionLocal, SlaDaily, engine

Base.metadata.create_all(engine)
DAY = date.today() - timedelta(days=1)


def _fresh_device(hostname: str, lm_id: int) -> int:
    with SessionLocal() as db:
        d = Device(hostname=hostname, management_ip="10.0.0.9", lm_device_id=lm_id,
                   active=True, match_status="Matched")
        db.add(d)
        db.commit()
        db.refresh(d)
        return d.id


def _store_good_day(dev_id: int) -> None:
    with SessionLocal() as db:
        db.add(SlaDaily(device_id=dev_id, day=DAY, expected_minutes=1440, observed_minutes=1440,
                        up_minutes=1440, availability=100.0, coverage=100.0, source="Ping"))
        db.commit()


class _DiscoverableClient:
    """Datasource discovery succeeds; subclasses decide what the data call does."""
    def __init__(self, *a, **k):
        pass

    async def applied_datasources(self, device_id):
        return [{"id": 1, "dataSourceName": sla.get_settings().availability_source}]

    async def instances(self, device_id, hds_id):
        return [{"id": 11}]


class DeadLMClient(_DiscoverableClient):
    async def instance_data(self, *a, **k):
        raise RuntimeError("LogicMonitor unreachable (simulated outage)")


class WorkingLMClient(_DiscoverableClient):
    """Returns a well-formed window: 3 samples, 2 minutes apart, all reachable (loss 0)."""
    async def instance_data(self, device_id, hds, inst, start, end):
        base = start * 1000
        return {"dataPoints": [sla.LOSS_DATAPOINT],
                "time": [base, base + 120000, base + 240000],
                "values": [[0.0], [0.0], [0.0]]}


def test_logicmonitor_outage_must_not_destroy_stored_sla_history(monkeypatch):
    dev_id = _fresh_device("lmfail-sw-01", 90001)
    _store_good_day(dev_id)
    monkeypatch.setattr(sla, "LogicMonitorClient", DeadLMClient)

    with SessionLocal() as db:
        asyncio.run(sla.backfill_device(db.get(Device, dev_id), DAY, DAY, force=True))

    with SessionLocal() as db:
        row = db.query(SlaDaily).filter_by(device_id=dev_id, day=DAY).one()
    # The measured day must survive the outage completely untouched.
    assert row.observed_minutes == 1440
    assert row.up_minutes == 1440
    assert row.availability == 100.0
    assert row.coverage == 100.0


def test_logicmonitor_outage_does_not_fabricate_a_row_when_none_existed(monkeypatch):
    dev_id = _fresh_device("lmfail-sw-02", 90002)  # no stored day at all
    monkeypatch.setattr(sla, "LogicMonitorClient", DeadLMClient)

    with SessionLocal() as db:
        asyncio.run(sla.backfill_device(db.get(Device, dev_id), DAY, DAY, force=True))

    with SessionLocal() as db:
        row = db.query(SlaDaily).filter_by(device_id=dev_id, day=DAY).one_or_none()
    # No evidence must stay "no row", never a fabricated zero-observed row.
    assert row is None


def test_successful_collection_still_writes_the_measured_day(monkeypatch):
    """Guard against over-correcting: the happy path must still persist real measurements."""
    dev_id = _fresh_device("lmok-sw-03", 90003)
    monkeypatch.setattr(sla, "LogicMonitorClient", WorkingLMClient)

    with SessionLocal() as db:
        result = asyncio.run(sla.backfill_device(db.get(Device, dev_id), DAY, DAY, force=True))

    assert result["written"] >= 1
    assert result["skipped_no_evidence"] == 0
    with SessionLocal() as db:
        row = db.query(SlaDaily).filter_by(device_id=dev_id, day=DAY).one()
    assert row.observed_minutes > 0
    assert row.availability == 100.0  # every sample had loss < 100 -> fully up
