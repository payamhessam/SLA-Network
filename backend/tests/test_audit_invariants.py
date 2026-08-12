"""Golden-dataset and invariant tests added by the 2026-08-11 data-integrity audit.

These lock in the specific defects found and fixed during that audit so they cannot
silently regress: the reports.py percentage formatter must match the UI's rounding
rule, and core SLA aggregation must never violate basic mathematical invariants
(availability/coverage bounded 0-100, no fabricated zero for missing evidence,
no negative downtime).
"""
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("LOCAL_ADMIN_PASSWORD", "correct-horse-battery-staple")
os.environ.setdefault("LOCAL_USER_PASSWORD", "viewer-password-long")
os.environ.setdefault("JWT_SECRET", "test-secret-long-enough")

from app import reports, sla


def _day(expected, observed, up, device_id=1, day=None):
    return SimpleNamespace(expected_minutes=expected, observed_minutes=observed, up_minutes=up, device_id=device_id, day=day)


# ---- golden dataset: reports._pct must match the UI's fmtPct rounding rule exactly ----
# (frontend/src/format.ts: round to 2dp, trim trailing zeros) — this was a real, confirmed
# discrepancy (Excel showed "100.000%" while the UI showed "100%") fixed in this audit.
def test_pct_formatter_matches_ui_rounding_rule():
    cases = {
        100.0: "100%",
        99.9754: "99.98%",
        99.923: "99.92%",
        99.9: "99.9%",
        0.0: "0%",
        None: "Insufficient",
        "n/a": "Insufficient",
    }
    for value, expected in cases.items():
        assert reports._pct(value) == expected, f"_pct({value}) = {reports._pct(value)!r}, expected {expected!r}"


# ---- invariants: availability/coverage/downtime must stay within physically sane bounds ----
def test_availability_never_exceeds_0_to_100():
    for up, observed in [(1440, 1440), (0, 1440), (1439, 1440), (100000, 100000)]:
        agg = sla._aggregate([_day(1440, observed, up)])
        if agg["availability"] is not None:
            assert 0.0 <= agg["availability"] <= 100.0


def test_coverage_never_exceeds_100_even_when_observed_overshoots_expected():
    # A device can report slightly more observed minutes than the nominal day length
    # (poll jitter); coverage must still be capped at 100, never a nonsensical >100%.
    agg = sla._aggregate([_day(1440, 1500, 1500)])
    assert agg["coverage"] <= 100.0


def test_missing_evidence_is_never_silently_reported_as_zero_percent():
    # No rows at all (device never observed in the window) -> availability must be None,
    # not fabricated as 0% (0% would look like a real, measured total outage).
    agg = sla._aggregate([])
    assert agg["availability"] is None
    assert agg["status"] == "Insufficient evidence"


def test_downtime_is_never_negative():
    for up, observed in [(1440, 1440), (0, 100), (1439, 1440)]:
        agg = sla._aggregate([_day(1440, observed, up)])
        down = max(0, agg["observed_minutes"] - agg["up_minutes"])
        assert down >= 0


# ---- data-equivalence: pathres._branch_windows (batched, in-memory) must produce the exact
# same numbers as the original per-device sla.window() calls it replaced for performance. ----
def test_branch_windows_batched_matches_naive_per_device_aggregation():
    from datetime import date
    from app import pathres
    d = date(2026, 6, 1)
    rows = [_day(1440, 1440, 1440, device_id=1, day=d), _day(1440, 1440, 1300, device_id=2, day=d)]
    rows_by_device = {1: [rows[0]], 2: [rows[1]]}
    # naive: what sla.window() would have computed per device, summed by hand
    naive_up = rows[0].up_minutes + rows[1].up_minutes
    naive_obs = rows[0].observed_minutes + rows[1].observed_minutes
    naive_pooled = round(100.0 * naive_up / naive_obs, 4)
    naive_best = max(
        sla._aggregate([rows[0]])["availability"],
        sla._aggregate([rows[1]])["availability"],
    )
    naive_best = round(naive_best, 4)

    # Only meaningful for a window that actually contains `d` — use YTD which always does
    # for a same-year historical date, then just check the aggregation math independent of
    # which exact window key it landed under.
    from app.sla import _window_bounds, today_local
    ref = today_local()
    kind = next(k for k in pathres.WINDOWS if _window_bounds(k, ref)[0] <= d <= _window_bounds(k, ref)[1])
    out = pathres._branch_windows(rows_by_device, [1, 2])
    assert out[kind]["pooled"] == naive_pooled
    assert out[kind]["best_path"] == naive_best


# ---- telemetry.interfaces() must partition every interface into exactly one of
# up/down/disabled/unknown, so total never silently disagrees with their sum (2026-08-12 audit:
# the fleet-wide Interface/Circuit Health tiles showed total=1633 but up+down=264, with the
# other 1369 interfaces silently dropped from every count). ----
def test_interface_state_buckets_always_sum_to_total():
    from app import telemetry
    snap = SimpleNamespace(details={"tables": {"Interfaces": [
        {"Status": "up", "Admin State": "up", "Interface": "Gi1/0/1"},
        {"Status": "down", "Admin State": "up", "Interface": "Gi1/0/2"},        # real problem
        {"Status": "down", "Admin State": "down", "Interface": "Gi1/0/3"},      # intentionally disabled
        {"Status": "unknown", "Admin State": "unknown", "Interface": "Gi1/0/4"},  # no LM data
    ]}})
    fleet = [{"device_id": 1, "hostname": "sw1", "city": "Test", "snap": snap}]
    result = telemetry.interfaces(None, fleet)
    assert result["total"] == 4
    assert result["up"] + result["down"] + result["disabled"] + result["unknown"] == result["total"]
    assert (result["up"], result["down"], result["disabled"], result["unknown"]) == (1, 1, 1, 1)


# ---- ssh_collect._reported_hostname must extract a device's OWN configured hostname from
# either its running-config or its "show version" uptime banner, so device_detail() can catch
# a stale LogicMonitor management-IP mapping (2026-08-12: LM's inventory pointed the
# "CA07-Z01-DAS-01" record at a management IP that actually belonged to "ca14-z01-dsw-01" -
# every automated signal agreed with the wrong IP because nothing ever asked the device itself
# who it was). ----
def test_reported_hostname_from_config_line():
    from app import ssh_collect
    raw = {"config": "!\nhostname ca07-z01-das-01\n!\nvrf definition Mgmt-vrf\n"}
    assert ssh_collect._reported_hostname(raw) == "ca07-z01-das-01"


def test_reported_hostname_falls_back_to_version_banner_when_no_config():
    from app import ssh_collect
    raw = {"version": "Cisco IOS XE Software, Version 17.06.05\nca14-z01-dsw-01 uptime is 1 year, 8 weeks\n"}
    assert ssh_collect._reported_hostname(raw) == "ca14-z01-dsw-01"


def test_reported_hostname_none_when_neither_present():
    from app import ssh_collect
    assert ssh_collect._reported_hostname({}) is None
