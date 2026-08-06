import os
from io import BytesIO

os.environ.setdefault("DATABASE_URL","sqlite:///:memory:")
os.environ.setdefault("LOCAL_ADMIN_PASSWORD","correct-horse-battery-staple")
os.environ.setdefault("LOCAL_USER_PASSWORD","viewer-password-long")
os.environ.setdefault("JWT_SECRET","test-secret-long-enough")

from fastapi.testclient import TestClient
from app.main import app


def auth(client, username="admin", password="correct-horse-battery-staple"):
    token=client.post("/api/v1/auth/login",json={"username":username,"password":password}).json()["access_token"]
    return {"Authorization":f"Bearer {token}"}


def test_inventory_naming_sites_zones_permissions_and_audit():
    with TestClient(app) as client:
        admin=auth(client);viewer=auth(client,"user","viewer-password-long")
        sites=client.get("/api/v1/settings/sites",headers=admin).json()
        assert any(x["site_code"]=="CAD03" and x["city"]=="Quebec City" for x in sites)
        assert len(client.get("/api/v1/settings/zones",headers=admin).json())==9
        types={x["type_code"]:x["derived_role"] for x in client.get("/api/v1/settings/device-types",headers=admin).json()}
        assert types=={"DSW":"Distribution","ASW":"Access","RTR":"Router","WAP":"Access Point"}
        expected=[("Z01","DSW","01","CAD03-Z01-DSW-01","Distribution"),("Z02","ASW","02","CAD03-Z02-ASW-02","Access"),("Z01","RTR","01","CAD03-Z01-RTR-01","Router"),("Z03","WAP","03","CAD03-Z03-WAP-03","Access Point")]
        for zone,dtype,number,name,role in expected:
            result=client.post("/api/v1/inventory/preview-name",headers=admin,json={"site_code":"CAD03","zone":zone,"device_type":dtype,"device_number":number})
            assert result.status_code==200 and result.json()["generated_name"]==name and result.json()["role"]==role and result.json()["city"]=="Quebec City"
        assert client.post("/api/v1/settings/sites",headers=viewer,json={"site_code":"ZZ99","city":"Denied","country":"Canada"}).status_code==403
        assert client.post("/api/v1/settings/sites",headers=admin,json={"site_code":"CAD03","city":"Duplicate","country":"Canada"}).status_code==409


def test_device_duplicates_template_and_transactional_import():
    with TestClient(app) as client:
        h=auth(client);body={"site_code":"CAD03","zone":"Z09","device_type":"RTR","device_number":"98","criticality":"High","management_ip":"192.0.2.98"}
        created=client.post("/api/v1/inventory/devices",headers=h,json=body)
        assert created.status_code==201 and created.json()["generated_name"]=="CAD03-Z09-RTR-98"
        assert client.post("/api/v1/inventory/devices",headers=h,json=body).status_code==409
        duplicate_ip={**body,"device_number":"97"}
        assert client.post("/api/v1/inventory/devices",headers=h,json=duplicate_ip).status_code==409
        template=client.get("/api/v1/inventory/import/template",headers=h)
        assert template.status_code==200 and template.content[:2]==b"PK"
        csv=b"SiteCode,Zone,DeviceType,DeviceNumber,Criticality,Enabled\nCAD03,Z08,WAP,96,Medium,true\nBAD,Z01,DSW,01,Medium,true\n"
        validation=client.post("/api/v1/inventory/import/validate",headers=h,files={"file":("test.csv",BytesIO(csv),"text/csv")}).json()
        assert validation["summary"]=={"total":2,"ready":1,"errors":1}
        assert client.post(f"/api/v1/inventory/import/commit?job_id={validation['job_id']}&mode=all_or_nothing",headers=h).status_code==422
        result=client.post(f"/api/v1/inventory/import/commit?job_id={validation['job_id']}&mode=valid_rows_only",headers=h).json()
        assert result["imported"]==1 and result["skipped"]==1
