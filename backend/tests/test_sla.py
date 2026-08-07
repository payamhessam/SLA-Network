import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("LOCAL_ADMIN_PASSWORD", "correct-horse-battery-staple")
os.environ.setdefault("LOCAL_USER_PASSWORD", "viewer-password-long")
os.environ.setdefault("JWT_SECRET", "test-secret-long-enough")

from app import resilience, sla


def _day(expected, observed, up):
    return SimpleNamespace(expected_minutes=expected, observed_minutes=observed, up_minutes=up)


def test_aggregate_gates_on_coverage_and_never_fabricates_zero():
    # Full coverage, one bad minute -> real availability
    good = sla._aggregate([_day(1440, 1440, 1439)])
    assert good["status"] == "ok" and round(good["availability"], 3) == 99.931
    # Coverage below 90% -> Insufficient, availability withheld (not 0/100)
    thin = sla._aggregate([_day(1440, 100, 100)])
    assert thin["status"] == "Insufficient evidence" and thin["availability"] is None
    # No rows -> Insufficient
    assert sla._aggregate([])["availability"] is None


def test_counts_from_samples_handles_descending_time():
    # LM returns `time` newest-first; interval must come from abs() deltas (60s here)
    data = {"dataPoints": ["PingLossPercent"], "time": [120000, 60000, 0], "values": [[0], [0], [100]]}
    obs, up, ok = sla._counts_from_samples(data)
    assert ok and round(obs) == 3 and round(up) == 2
    # Missing datapoint -> no observation, not a fabricated number
    o, u, k = sla._counts_from_samples({"dataPoints": [], "values": []})
    assert k is False and o == 0.0 and u == 0.0


def _signals(uplinks, stack, dual_power, stack_known=True):
    return {"redundant_uplinks": uplinks, "stack_members": stack, "stack_known": stack_known,
            "dual_power": dual_power, "power_supplies": 2 if dual_power else 1, "max_link_bps": None}


def test_tier_banding():
    assert resilience.tier_of(_signals(1, 1, False), None)[0] == "Tier I"
    assert resilience.tier_of(_signals(1, 1, True), None)[0] == "Tier II"
    assert resilience.tier_of(_signals(2, 2, True), 99.9)[0] == "Tier III"
    assert resilience.tier_of(_signals(2, 2, True), 99.996)[0] == "Tier IV"
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
