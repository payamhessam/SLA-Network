import os
os.environ["DATABASE_URL"]="sqlite:///:memory:"
os.environ["LOCAL_ADMIN_PASSWORD"]="correct-horse-battery-staple"
os.environ.setdefault("LOCAL_USER_PASSWORD","viewer-password-long")
os.environ["JWT_SECRET"]="test-secret-long-enough-32-bytes"
from fastapi.testclient import TestClient
from app.collection import numeric, percentile
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
def test_add_ap_and_remove():
    with TestClient(app) as client:
        h={"Authorization":f"Bearer {token(client)}"}
        r=client.post("/api/v1/devices",headers=h,json={"hostname":"AP-01","management_ip":"10.0.0.1","model":"C9130AXI"})
        assert r.status_code==201 and r.json()["device_type"]=="access_point"
        assert client.delete(f"/api/v1/devices/{r.json()['id']}",headers=h).status_code==204
