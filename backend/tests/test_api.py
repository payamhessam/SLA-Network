import os
os.environ["DATABASE_URL"]="sqlite:///:memory:"
os.environ["LOCAL_ADMIN_PASSWORD"]="correct-horse-battery-staple"
os.environ.setdefault("LOCAL_USER_PASSWORD","viewer-password-long")
os.environ["JWT_SECRET"]="test-secret-long-enough-32-bytes"
from fastapi.testclient import TestClient
from app.collection import numeric, percentile
from app.access_points import MAX_AP_UPLOAD_BYTES, _validate_workbook_bytes, neighbor_evidence_is_fresh
from app.auth import issue_token
from app.db import SessionLocal, WanObservation, WanRouter
from app.main import app

def token(client):
    return client.post("/api/v1/auth/login",json={"username":"admin","password":"correct-horse-battery-staple"}).json()["access_token"]
def test_health():
    with TestClient(app) as client: assert client.get("/api/v1/health").status_code==200
def test_auth_required():
    with TestClient(app) as client: assert client.get("/api/v1/devices").status_code==401
def test_logicmonitor_numeric_strings_are_normalized_without_fabricating_missing_values():
    assert numeric("1.5") == 1.5
    assert numeric("not monitored") is None
    assert percentile(["1", 2, None], .95) == 2.0


def test_access_point_upload_rejects_oversized_content_before_parsing():
    from fastapi import HTTPException
    try:
        _validate_workbook_bytes(b"x" * (MAX_AP_UPLOAD_BYTES + 1))
    except HTTPException as error:
        assert error.status_code == 413
    else:
        raise AssertionError("oversized workbook was accepted")


def test_stale_switch_evidence_never_becomes_an_offline_access_point():
    from datetime import datetime, timedelta, timezone
    assert neighbor_evidence_is_fresh(datetime.now(timezone.utc) - timedelta(hours=3)) is False


def test_provider_service_level_is_a_separate_coverage_gated_report():
    from datetime import datetime, timedelta, timezone
    with TestClient(app) as client:
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            router = WanRouter(display_name="Provider A - Toronto", provider="Provider A", site_label="Toronto", status="Critical", last_sync=now)
            db.add(router); db.flush()
            db.add_all([
                WanObservation(wan_router_id=router.id, collected_at=now - timedelta(minutes=60), status="Healthy"),
                WanObservation(wan_router_id=router.id, collected_at=now - timedelta(minutes=30), status="Critical"),
                WanObservation(wan_router_id=router.id, collected_at=now, status="Critical"),
            ])
            db.commit()
        response = client.get("/api/v1/wan/service-level", headers={"Authorization": f"Bearer {issue_token('api-test')}"})
        assert response.status_code == 200
        site = next(item for item in response.json()["sites"] if item["site"] == "Toronto")
        assert site["state"] == "Full outage"
        assert site["links"][0]["availability"] is not None
def test_add_ap_and_remove():
    with TestClient(app) as client:
        h={"Authorization":f"Bearer {token(client)}"}
        r=client.post("/api/v1/devices",headers=h,json={"hostname":"AP-01","management_ip":"10.0.0.1","model":"C9130AXI"})
        assert r.status_code==201 and r.json()["device_type"]=="access_point"
        assert client.delete(f"/api/v1/devices/{r.json()['id']}",headers=h).status_code==204
