import os
os.environ["DATABASE_URL"]="sqlite:///:memory:"
os.environ["LOCAL_ADMIN_PASSWORD"]="correct-horse-battery-staple"
os.environ["JWT_SECRET"]="test-secret-long-enough"
from fastapi.testclient import TestClient
from app.main import app

client=TestClient(app)
def token():
    return client.post("/api/v1/auth/login",json={"username":"admin","password":"correct-horse-battery-staple"}).json()["access_token"]
def test_health(): assert client.get("/api/v1/health").status_code==200
def test_auth_required(): assert client.get("/api/v1/devices").status_code==401
def test_add_ap_and_remove():
    h={"Authorization":f"Bearer {token()}"}
    r=client.post("/api/v1/devices",headers=h,json={"hostname":"AP-01","management_ip":"10.0.0.1","model":"C9130AXI"})
    assert r.status_code==201 and r.json()["device_type"]=="access_point"
    assert client.delete(f"/api/v1/devices/{r.json()['id']}",headers=h).status_code==204
