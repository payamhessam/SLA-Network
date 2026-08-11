import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("LOCAL_ADMIN_PASSWORD", "correct-horse-battery-staple")
os.environ.setdefault("LOCAL_USER_PASSWORD", "viewer-password-long")
os.environ.setdefault("JWT_SECRET", "test-secret-long-enough")

from app import overview


def _dev(band="Medium", status="Healthy", match="Matched", snap=None):
    return {"device_id": 1, "hostname": "CA05-Z01-DSW-01", "city": "Delta", "model": "C9300",
            "band": band, "status": status, "match_status": match, "snap": snap}


def test_criticality_bands_and_counts():
    # The inventory schema offers exactly four business-criticality values
    # (Literal["Low","Medium","High","Critical"], default "Medium"). Each must keep its own
    # identity: an earlier version collapsed Medium AND Low into an invented band called
    # "Standard", so the Overview ring reported "Standard 40" for a fleet that was really
    # sitting entirely on the untouched "Medium" default.
    assert overview._band("Critical") == "Critical"
    assert overview._band("High") == "High"
    assert overview._band("Medium") == "Medium"
    assert overview._band("Low") == "Low"
    assert overview._band("critical") == "Critical"          # case-insensitive
    assert overview._band(None) == "Medium"                  # unset -> schema default
    assert overview._band("Standard") == "Medium"            # unknown -> schema default
    fleet = [_dev("Critical", "Healthy"), _dev("High", "Warning"), _dev("Medium", "Critical"), _dev("Low", "Unknown")]
    c = overview.criticality(None, fleet)
    assert c["total"] == 4
    assert c["bands"] == {"Critical": 1, "High": 1, "Medium": 1, "Low": 1}
    assert c["degraded"] == 1  # the Warning device
    assert c["unreachable"] == 2  # Critical + Unknown


def test_throughput_is_not_fabricated():
    t = overview.throughput(None, [])
    assert t["available"] is False and t["value"] is None


def test_device_problem_severity():
    healthy = SimpleNamespace(cpu=5, memory=20, temperature=30, status="Healthy", details={"uptime": 864000})
    sev, label, value, status = overview._device_problem(_dev(status="Healthy", snap=healthy))
    assert sev == 0 and status == "OK" and value == "10d"

    hot_cpu = SimpleNamespace(cpu=92, memory=20, temperature=30, status="Warning", details={})
    sev, label, value, status = overview._device_problem(_dev(status="Warning", snap=hot_cpu))
    assert sev == 2 and status == "HIGH CPU" and value == "92%"

    crit = SimpleNamespace(cpu=5, memory=5, temperature=5, status="Critical",
                           details={"tables": {"Environmental and PoE": [{"State": "Fault", "Category": "Fan"}]}})
    sev, label, value, status = overview._device_problem(_dev(status="Critical", snap=crit))
    assert sev == 3 and status == "CRITICAL"

    # No snapshot / not matched -> unknown, never fabricated as healthy
    sev, label, value, status = overview._device_problem(_dev(status="Unknown", match="Collection failed", snap=None))
    assert status == "UNKNOWN"
