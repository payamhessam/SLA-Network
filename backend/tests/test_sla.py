import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("LOCAL_ADMIN_PASSWORD", "correct-horse-battery-staple")
os.environ.setdefault("LOCAL_USER_PASSWORD", "viewer-password-long")
os.environ.setdefault("JWT_SECRET", "test-secret-long-enough-32-bytes")

from app import resilience, sla
from app.collection import latest


def _day(expected, observed, up, device_id=1, day=None):
    return SimpleNamespace(expected_minutes=expected, observed_minutes=observed, up_minutes=up, device_id=device_id, day=day)


def test_aggregate_gates_on_coverage_and_never_fabricates_zero():
    # Full coverage, one bad minute -> real availability
    good = sla._aggregate([_day(1440, 1440, 1439)])
    assert good["status"] == "ok" and round(good["availability"], 3) == 99.931
    # Coverage below 90% -> Insufficient, availability withheld (not 0/100)
    thin = sla._aggregate([_day(1440, 100, 100)])
    assert thin["status"] == "Insufficient evidence" and thin["availability"] is None
    # No rows -> Insufficient
    assert sla._aggregate([])["availability"] is None


def test_aggregate_trims_pre_commissioning_days_so_new_sites_are_publishable():
    from datetime import date, timedelta
    jan1 = date(2026, 1, 1)
    # A device commissioned mid-window: 100 empty (un-monitored) days, then 20 fully-polled days.
    rows = [_day(1440, 0, 0, device_id=7, day=jan1 + timedelta(days=i)) for i in range(100)]
    rows += [_day(1440, 1440, 1440, device_id=7, day=jan1 + timedelta(days=120 + i)) for i in range(20)]
    agg = sla._aggregate(rows)
    # Leading zero-observed days are trimmed -> full coverage over the monitored period,
    # 100% availability, and the commissioning date is surfaced (not blanked as Insufficient).
    assert agg["status"] == "ok"
    assert agg["availability"] == 100.0
    assert agg["coverage"] == 100.0
    assert agg["first_observed"] == (jan1 + timedelta(days=120)).isoformat()


def test_aggregate_per_device_trim_does_not_penalise_mixed_group():
    from datetime import date, timedelta
    jan1 = date(2026, 1, 1)
    # One device present all window (high coverage) + one commissioned late (empty lead-in).
    old = [_day(1440, 1440, 1440, device_id=1, day=jan1 + timedelta(days=i)) for i in range(120)]
    new_empty = [_day(1440, 0, 0, device_id=2, day=jan1 + timedelta(days=i)) for i in range(110)]
    new_live = [_day(1440, 1440, 1439, device_id=2, day=jan1 + timedelta(days=120 + i)) for i in range(10)]
    agg = sla._aggregate(old + new_empty + new_live)
    # The late device's empty months must not drag the group below the coverage gate.
    assert agg["status"] == "ok"
    assert agg["coverage"] == 100.0


def test_counts_from_samples_handles_descending_time():
    # LM returns `time` newest-first; interval must come from abs() deltas (60s here)
    data = {"dataPoints": ["PingLossPercent"], "time": [120000, 60000, 0], "values": [[0], [0], [100]]}
    obs, up, ok = sla._counts_from_samples(data)
    assert ok and round(obs) == 3 and round(up) == 2
    # Missing datapoint -> no observation, not a fabricated number
    o, u, k = sla._counts_from_samples({"dataPoints": [], "values": []})
    assert k is False and o == 0.0 and u == 0.0


def test_missing_evidence_after_monitoring_started_reduces_coverage():
    from datetime import date, timedelta
    first = date(2026, 1, 1)
    rows = [_day(1440, 1440, 1440, device_id=7, day=first)]
    filled = sla._fill_missing_evidence(rows, first, first + timedelta(days=2), {7: first})
    agg = sla._aggregate(filled)
    assert agg["availability"] is None
    assert agg["coverage"] == round(100 / 3, 2)
    assert agg["missing_evidence_days"] == 2


def test_latest_uses_newest_timestamp_not_response_position():
    data = {"dataPoints": ["CPU"], "time": [3000, 2000, 1000], "values": [[90], [50], [10]]}
    assert latest(data) == {"CPU": 90}
    reordered = {"dataPoints": ["CPU"], "time": [1000, 3000, 2000], "values": [[10], [90], [50]]}
    assert latest(reordered) == {"CPU": 90}


def _signals(uplinks, stack, dual_power, stack_known=True):
    return {"redundant_uplinks": uplinks, "stack_members": stack, "stack_known": stack_known,
            "dual_power": dual_power, "power_supplies": 2 if dual_power else 1, "max_link_bps": None}


def test_tier_banding():
    assert resilience.tier_of(_signals(1, 1, False), None)[0] == "Tier I"
    assert resilience.tier_of(_signals(1, 1, True), None)[0] == "Tier II"
    assert resilience.tier_of(_signals(2, 2, True), 99.9)[0] == "Tier II"
    assert resilience.tier_of(_signals(2, 2, True), 99.996)[0] == "Tier II"
    assert resilience.tier_of(_signals(0, 1, False, stack_known=False), None)[0] == "Insufficient"


def test_device_signals_reads_redundancy_evidence():
    details = {"tables": {
        "Inventory": [
            {"Description": "C9300-24U", "PID": "C9300-24U", "Serial Number": "FVH1"},
            {"Description": "C9300-24U", "PID": "C9300-24U", "Serial Number": "FVH2"},
        ],
        "Environmental and PoE": [
            {"Category": "Power Supply", "State": "Normal"},
            {"Category": "Power Supply", "State": "Normal"},
        ],
        "CDP-LLDP Neighbors": [
            {"Neighbor": "ca14-z01-dsw-01.medline.com"},
            {"Neighbor": "ca14-z01-dsw-02.medline.com"},
            {"Neighbor": "ca14-z03-wap-10"},
        ],
    }}
    s = resilience.device_signals(details)
    assert s["stack_members"] == 2 and s["dual_power"] is True and s["redundant_uplinks"] == 2
