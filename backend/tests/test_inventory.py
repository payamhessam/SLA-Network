import os
import zipfile
from io import BytesIO
from openpyxl import Workbook

os.environ.setdefault("DATABASE_URL","sqlite:///:memory:")
os.environ.setdefault("LOCAL_ADMIN_PASSWORD","correct-horse-battery-staple")
os.environ.setdefault("LOCAL_USER_PASSWORD","viewer-password-long")
os.environ.setdefault("JWT_SECRET","test-secret-long-enough-32-bytes")

from fastapi.testclient import TestClient
from app.inventory import physical_switch_count
from app.main import app


def auth(client, username="admin", password="correct-horse-battery-staple"):
    token=client.post("/api/v1/auth/login",json={"username":username,"password":password}).json()["access_token"]
    return {"Authorization":f"Bearer {token}"}


def test_physical_switch_count_uses_unique_chassis_inventory_evidence():
    details={"tables":{"Inventory":[
        {"Description":"StackPort2/1","PID":"Not available from LogicMonitor","Serial Number":"MOG2743A5V2"},
        {"Description":"C9300-24U","PID":"C9300-24U","Serial Number":"FVH2743L1C7"},
        {"Description":"C9300-24U","PID":"C9300-24U","Serial Number":"FVH2743L14P"},
        {"Description":"StackPort2/2","PID":"Not available from LogicMonitor","Serial Number":"MOG2744A1K5"},
        {"Description":"Switch 2 - Power Supply A","PID":"Not available from LogicMonitor","Serial Number":"ART2731S7K"},
    ]}}
    assert physical_switch_count(details)==(2,"Inventory chassis serials")
    assert physical_switch_count({})==(None,"Not monitored")


def test_inventory_naming_sites_zones_permissions_and_audit():
    with TestClient(app) as client:
        admin=auth(client);viewer=auth(client,"user","viewer-password-long")
        sites=client.get("/api/v1/settings/sites",headers=admin).json()
        assert any(x["site_code"]=="CAD03" and x["city"]=="Quebec City" for x in sites)
        assert len(client.get("/api/v1/settings/zones",headers=admin).json())==9
        types={x["type_code"]:x["derived_role"] for x in client.get("/api/v1/settings/device-types",headers=admin).json()}
        assert types=={"DSW":"Distribution","ASW":"Access","RTR":"Router"}
        expected=[("Z01","DSW","01","CAD03-Z01-DSW-01","Distribution"),("Z02","ASW","02","CAD03-Z02-ASW-02","Access"),("Z01","RTR","01","CAD03-Z01-RTR-01","Router")]
        for zone,dtype,number,name,role in expected:
            result=client.post("/api/v1/inventory/preview-name",headers=admin,json={"site_code":"CAD03","zone":zone,"device_type":dtype,"device_number":number})
            assert result.status_code==200 and result.json()["generated_name"]==name and result.json()["role"]==role and result.json()["city"]=="Quebec City"
        assert client.post("/api/v1/inventory/preview-name",headers=admin,json={"site_code":"CAD03","zone":"Z03","device_type":"WAP","device_number":"03"}).status_code==422
        assert client.post("/api/v1/settings/device-types",headers=admin,json={"type_code":"WAP","description":"Wireless Access Point","derived_role":"Access Point","enabled":True}).status_code==422
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
        csv=b"SiteCode,Zone,DeviceType,DeviceNumber,Criticality,Enabled\nCAD03,Z08,DSW,96,Medium,true\nBAD,Z01,DSW,01,Medium,true\n"
        validation=client.post("/api/v1/inventory/import/validate",headers=h,files={"file":("test.csv",BytesIO(csv),"text/csv")}).json()
        assert validation["summary"]=={"total":2,"ready":1,"errors":1}
        assert client.post(f"/api/v1/inventory/import/commit?job_id={validation['job_id']}&mode=all_or_nothing",headers=h).status_code==422
        result=client.post(f"/api/v1/inventory/import/commit?job_id={validation['job_id']}&mode=valid_rows_only",headers=h).json()
        assert result["imported"]==1 and result["skipped"]==1


def test_access_point_import_validation_and_replace():
    with TestClient(app) as client:
        h=auth(client);wb=Workbook();ws=wb.active
        ws.append(["AP Name","AP Model","IP Address","AP Radio MAC","Ethernet MAC","Serial Number","Site Tag","Ignored Column"])
        ws.append(["cad03-z01-wap-001","C9130AXI","10.7.222.50","0011.2233.4455","0066.7788.99aa","SERIAL001","CAD03","ignored"])
        normal=BytesIO();wb.save(normal);normal.seek(0)
        content=BytesIO()
        with zipfile.ZipFile(normal) as source, zipfile.ZipFile(content,"w") as target:
            for name in source.namelist():
                value=source.read(name)
                if name=="xl/worksheets/sheet1.xml": value=value.replace(b'<dimension ref="A1:H2"',b'<dimension ref="A1"')
                target.writestr(name,value)
        content.seek(0)
        validation=client.post("/api/v1/access-points/import/validate",headers=h,files={"file":("aps.xlsx",content,"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert validation.status_code==200 and validation.json()["summary"]=={"total":1,"ready":1,"warnings":0,"errors":0,"review":0}
        result=client.post(f"/api/v1/access-points/import/{validation.json()['job_id']}/commit?mode=replace",headers=h)
        assert result.status_code==200 and result.json()["records"]==1
        inventory=client.get("/api/v1/access-points",headers=h).json()
        assert inventory["summary"]["total"]==1 and inventory["items"][0]["city"]=="Quebec City" and inventory["items"][0]["status"]=="Offline"
