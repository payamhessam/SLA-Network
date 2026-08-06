import csv, io, logging, re, time, uuid
from collections import defaultdict
from pathlib import Path
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from slowapi import Limiter
from slowapi.util import get_remote_address

from .auth import administrator, authenticate, current_user, issue_token
from .collection import collect_logicmonitor_device
from .config import get_settings
from .db import AuditEvent, Base, Device, SessionLocal, Snapshot, engine, session
from .logicmonitor import LogicMonitorClient, match_device
from .reporting import create_report
from .schemas import DeviceCreate, DeviceOut, DeviceUpdate, Login

settings = get_settings(); limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Enterprise Network Health and SLA", version="1.0.0", docs_url="/docs")
app.state.limiter = limiter
app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in settings.allowed_origins.split(",")], allow_credentials=False, allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Authorization", "Content-Type", "X-Request-ID"])


@app.on_event("startup")
def startup(): Base.metadata.create_all(engine)


@app.middleware("http")
async def secure_headers(request: Request, call_next):
    request.state.request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    response = await call_next(request)
    response.headers.update({"X-Content-Type-Options":"nosniff", "X-Frame-Options":"DENY", "Referrer-Policy":"no-referrer", "Permissions-Policy":"camera=(), microphone=(), geolocation=()", "Content-Security-Policy":"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'", "X-Request-ID":request.state.request_id})
    return response


def audit(db, actor, action, target, details=None): db.add(AuditEvent(actor=actor, action=action, target=target, details=details or {}))


@app.post("/api/v1/auth/login")
@limiter.limit("5/minute")
def login(request: Request, body: Login):
    role = authenticate(body.username, body.password)
    if not role: raise HTTPException(401, "Invalid credentials")
    return {"access_token": issue_token(body.username, role), "token_type": "bearer", "role": role, "expires_in_days": 14}


@app.get("/api/v1/auth/me")
def me(user=Depends(current_user)):
    return {"username": user["sub"], "role": user["role"], "expires_at": user["exp"]}


@app.get("/api/v1/health")
def health(db: Session = Depends(session)):
    db.execute(select(func.count(Device.id))).scalar()
    return {"status":"ok", "database":"connected", "logicmonitor":"configured" if settings.lm_portal_url else "not configured", "version":"1.0.0"}


@app.get("/api/v1/dashboard")
def dashboard(user=Depends(current_user), db: Session = Depends(session)):
    devices = db.scalars(select(Device)).all(); counts = defaultdict(int)
    for d in devices: counts["active" if d.active else "inactive"] += 1; counts[d.device_type] += 1; counts[d.match_status] += 1
    return {"devices":len(devices), "counts":counts, "sla":{"actual":None,"target":settings.sla_target,"coverage":0,"status":"Baseline pending"}, "last_collection":None}


@app.get("/api/v1/devices", response_model=list[DeviceOut])
def devices(user=Depends(current_user), db: Session = Depends(session)): return db.scalars(select(Device).order_by(Device.hostname)).all()


DETAIL_TABLES = {
    "Health Data": ["Collected", "Status", "Score", "Model", "OS", "Serial", "MAC", "Uptime", "Reload Reason", "Restart Time", "CPU 5s %", "CPU 1m %", "CPU 5m %", "Memory Total", "Memory Used", "Memory %", "Max Temp C", "PoE Available W", "PoE Used W", "PoE Ports On"],
    "Ping Quality": ["24h Availability", "7d Availability", "Loss %", "Min Latency", "Avg Latency", "Max Latency", "P95 Latency", "Jitter", "Probe Count", "Trend"],
    "Interfaces": ["Interface", "Description", "Status", "VLAN", "Duplex", "Speed", "Type", "Align Errors", "FCS/CRC Errors", "TX Errors", "RX Errors", "Undersize", "Out Discards"],
    "VLANs": ["VLAN ID", "Name", "Status", "Ports"],
    "CDP-LLDP Neighbors": ["Protocol", "Local Interface", "Neighbor", "Management IP", "Platform/Model", "Remote Port"],
    "Inventory": ["Component", "Description", "PID", "VID", "Serial Number"],
    "Spanning Tree": ["Instance", "Blocking", "Listening", "Learning", "Forwarding", "Active"],
    "Environmental and PoE": ["Category", "Switch/Module", "Component/Interface", "State", "Temperature C", "Fan RPM", "Watts", "Details"],
    "Alerts": ["Severity", "Source", "Instance", "Message", "Age", "Acknowledged"],
    "Configuration Backups": ["Running Config", "Running Saved", "Startup Config", "Startup Saved", "Collected"],
    "Monitoring Gaps": ["Metric", "Reason", "Recommended Action"],
    "Collection Details": ["DataSource", "Instance", "Success", "Latest Data", "Error"],
}


@app.get("/api/v1/devices/{device_id}/detail")
def device_detail(device_id: int, user=Depends(current_user), db: Session = Depends(session)):
    item = db.get(Device, device_id)
    if not item: raise HTTPException(404, "Device not found")
    latest = db.scalar(select(Snapshot).where(Snapshot.device_id == device_id).order_by(Snapshot.collected_at.desc()).limit(1))
    tables = []
    for name, columns in DETAIL_TABLES.items():
        rows = (latest.details.get("tables", {}).get(name, []) if latest and latest.details else [])
        if name == "Health Data" and latest:
            rows = [{"Collected": latest.collected_at, "Status": latest.status, "Score": latest.details.get("score"), "Model": item.model, "OS": latest.details.get("os_version"), "Serial": latest.details.get("serial"), "MAC": latest.details.get("mac"), "Uptime": latest.details.get("uptime"), "CPU 1m %": latest.cpu, "Memory %": latest.memory, "Max Temp C": latest.temperature}]
        elif name == "Ping Quality" and latest:
            ping = latest.details.get("ping", {})
            rows = [{"24h Availability": ping.get("availability_24h"), "7d Availability": "Baseline pending", "Loss %": ping.get("loss"), "Min Latency": ping.get("min"), "Avg Latency": ping.get("average"), "Max Latency": ping.get("max"), "P95 Latency": ping.get("p95"), "Jitter": "Not monitored", "Probe Count": "Not available from LogicMonitor", "Trend": "Baseline pending"}]
        elif name == "Monitoring Gaps" and latest:
            missing = [x for x in ("VLANs", "Spanning Tree", "Alerts") if not latest.details.get("tables", {}).get(x)]
            rows = [{"Metric": x, "Reason": "No applied LogicMonitor DataSource mapping", "Recommended Action": "Review Mapping and Coverage; do not use direct device access"} for x in missing]
        tables.append({"name": name, "columns": columns, "rows": rows, "evidence_status": "Available" if rows else ("Collection failed" if item.match_status == "Collection failed" else "Not available from LogicMonitor")})
    return {"device": DeviceOut.model_validate(item), "tables": tables, "source": "LogicMonitor read-only API"}


@app.post("/api/v1/devices", response_model=DeviceOut, status_code=201)
def add_device(body: DeviceCreate, user=Depends(administrator), db: Session = Depends(session)):
    if db.scalar(select(Device).where(func.lower(Device.hostname) == body.hostname.lower())): raise HTTPException(409, "Hostname already exists")
    values = body.model_dump()
    if values["model"] and re.search(r"(?:C)?91(?:20|30)", values["model"], re.I): values["device_type"] = "access_point"
    item=Device(**values); db.add(item); audit(db,user["sub"],"device.create",body.hostname); db.commit(); db.refresh(item); return item


@app.put("/api/v1/devices/{device_id}", response_model=DeviceOut)
def update_device(device_id:int, body:DeviceUpdate, user=Depends(administrator), db:Session=Depends(session)):
    item=db.get(Device,device_id)
    if not item: raise HTTPException(404,"Device not found")
    for key,value in body.model_dump().items(): setattr(item,key,value)
    audit(db,user["sub"],"device.update",item.hostname); db.commit(); db.refresh(item); return item


@app.delete("/api/v1/devices/{device_id}", status_code=204)
def remove_device(device_id:int, user=Depends(administrator), db:Session=Depends(session)):
    item=db.get(Device,device_id)
    if not item: raise HTTPException(404,"Device not found")
    audit(db,user["sub"],"device.delete",item.hostname); db.delete(item); db.commit()


@app.post("/api/v1/devices/import")
async def import_devices(file:UploadFile=File(...), user=Depends(administrator), db:Session=Depends(session)):
    if file.content_type not in {"text/csv","application/vnd.ms-excel","application/octet-stream"}: raise HTTPException(415,"CSV required")
    raw=await file.read(2_000_001)
    if len(raw)>2_000_000: raise HTTPException(413,"File exceeds 2 MB")
    try: rows=list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    except UnicodeDecodeError: raise HTTPException(422,"CSV must be UTF-8") from None
    if len(rows)>5000: raise HTTPException(413,"At most 5,000 rows")
    created=[]; errors=[]
    for number,row in enumerate(rows,2):
        try:
            body=DeviceCreate(hostname=row.get("Hostname", "").strip(), management_ip=(row.get("ManagementIP") or None), site=row.get("Site") or "Unassigned", role=row.get("Role") or "Access", criticality=row.get("Criticality") or "Medium", device_type=row.get("DeviceType") or "switch", model=row.get("Model") or None, notes=row.get("Notes") or None)
            if db.scalar(select(Device).where(func.lower(Device.hostname)==body.hostname.lower())): raise ValueError("duplicate hostname")
            values=body.model_dump()
            if values["model"] and re.search(r"(?:C)?91(?:20|30)",values["model"],re.I): values["device_type"]="access_point"
            db.add(Device(**values)); created.append(body.hostname)
        except Exception as exc: errors.append({"row":number,"error":str(exc)[:200]})
    audit(db,user["sub"],"device.bulk_import",file.filename or "upload",{"created":len(created),"errors":len(errors)}); db.commit()
    return {"created":len(created),"errors":errors}


@app.post("/api/v1/collections/run")
async def collect(user=Depends(current_user), db:Session=Depends(session)):
    if user["role"] not in {"Administrator","Network Engineer"}: raise HTTPException(403,"Collection permission required")
    client=LogicMonitorClient(); matched=0; collected=0; failures=[]
    for item in db.scalars(select(Device).where(Device.active.is_(True))).all():
        try:
            remote,method=await client.find_device(item.hostname,item.management_ip); item.match_status="Matched" if remote else method
            if remote:
                item.lm_device_id=remote.get("id"); matched+=1
                normalized=await collect_logicmonitor_device(client,item,remote)
                item.model=normalized.get("model") or item.model
                db.add(Snapshot(device_id=item.id,status=normalized["status"],availability=normalized["availability"],cpu=normalized["cpu"],memory=normalized["memory"],temperature=normalized["temperature"],details=normalized["details"])); collected+=1
        except Exception as exc:
            item.match_status="Collection failed"; failures.append({"hostname":item.hostname,"category":type(exc).__name__})
    audit(db,user["sub"],"collection.run","LogicMonitor",{"matched":matched,"collected":collected,"failures":len(failures)}); db.commit()
    return {"status":"completed" if not failures else "completed_with_errors","matched":matched,"collected":collected,"requested":db.scalar(select(func.count(Device.id)).where(Device.active.is_(True))),"failures":failures}


@app.post("/api/v1/reports/generate")
def report(user=Depends(current_user), db:Session=Depends(session)):
    path=create_report(db.scalars(select(Device)).all()); return {"filename":path.name,"download_url":f"/api/v1/reports/{path.name}"}


@app.get("/api/v1/reports/{filename}")
def download(filename:str, user=Depends(current_user)):
    if not re.fullmatch(r"Network_Health_[0-9_-]+\.xlsx",filename): raise HTTPException(400,"Invalid report name")
    path=Path(settings.report_dir)/filename
    if not path.is_file(): raise HTTPException(404,"Report not found")
    return FileResponse(path,filename=filename,media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/v1/settings")
def app_settings(user=Depends(administrator)):
    return {"sla_target":settings.sla_target,"coverage_threshold":settings.coverage_threshold,"stale_minutes":settings.stale_minutes,"logicmonitor_portal":settings.lm_portal_url,"credentials_configured":bool(settings.access_id and settings.access_key)}
