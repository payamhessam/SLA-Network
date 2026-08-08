"""FastAPI application entry point and HTTP API for the Network Health & SLA portal.

This module wires everything together: it builds the FastAPI app, applies security
headers and CORS, mounts the feature routers (inventory, access points, reports),
defines the authentication/collection/SLA/overview/telemetry/trends endpoints, and
starts the background schedulers (device refresh, SLA rollup, resilience, AP status,
SLA backfill) on startup. All analytics endpoints read from the local database; the
only outbound traffic to LogicMonitor is read-only, signed GET requests.
"""
import asyncio, csv, io, logging, re, time, uuid
from contextlib import suppress
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from pydantic import BaseModel, Field
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from slowapi import Limiter
from slowapi.util import get_remote_address

from .auth import administrator, authenticate, current_user, issue_token
from .collection import collect_logicmonitor_device
from .config import get_settings
from .db import AuditEvent, Base, Device, SessionLocal, Snapshot, SshFacts, engine, session
from .logicmonitor import LogicMonitorClient, match_device
from .inventory import router as inventory_router, seed_inventory
from .access_points import router as access_point_router, ap_status_loop
from .reporting import create_report
from .schemas import DeviceCreate, DeviceOut, DeviceUpdate, Login
from .switch_refresh import switch_refresh_loop
from .wan import router as wan_router, wan_refresh_loop, bootstrap_wan
from . import overview, reports, resilience, sla, ssh_collect, telemetry, trends

settings = get_settings(); limiter = Limiter(key_func=get_remote_address)
# Disable the interactive docs and OpenAPI schema in production (no API surface disclosure).
_docs_url = None if settings.is_production else "/docs"
app = FastAPI(title="Enterprise Network Health and SLA", version="1.0.0",
              docs_url=_docs_url, redoc_url=None, openapi_url=(None if settings.is_production else "/openapi.json"))
app.state.limiter = limiter
app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in settings.allowed_origins.split(",")], allow_credentials=False, allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Authorization", "Content-Type", "X-Request-ID"])
app.include_router(inventory_router)
app.include_router(access_point_router)
app.include_router(wan_router)


@app.on_event("startup")
async def startup():
    # Production safety gate: refuse to start with development defaults when
    # environment=production, so a real deployment can never run with dev credentials.
    issues = settings.production_issues()
    if issues:
        msg = "Insecure production configuration:\n  - " + "\n  - ".join(issues)
        if settings.is_production:
            raise RuntimeError(msg + "\nFix these before starting in production.")
        logging.getLogger("startup").warning("%s\n(development mode — not enforced)", msg)
    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            for column, ddl in (("status", "varchar(20) DEFAULT 'Offline'"), ("last_seen", "timestamptz"), ("connected_switch", "varchar(255)"), ("connected_interface", "varchar(255)"), ("last_status_check", "timestamptz")):
                conn.execute(text(f"ALTER TABLE access_point_inventory ADD COLUMN IF NOT EXISTS {column} {ddl}"))
            conn.execute(text("ALTER TABLE sites ADD COLUMN IF NOT EXISTS business_unit varchar(40) DEFAULT 'Unassigned'"))
            conn.execute(text("ALTER TABLE wan_routers ADD COLUMN IF NOT EXISTS city varchar(120) DEFAULT ''"))
            conn.execute(text("ALTER TABLE wan_routers ADD COLUMN IF NOT EXISTS province varchar(80) DEFAULT ''"))
    with SessionLocal() as db: seed_inventory(db)
    app.state.switch_refresh_task = asyncio.create_task(switch_refresh_loop())
    app.state.sla_rollup_task = asyncio.create_task(sla.sla_rollup_loop())
    app.state.resilience_task = asyncio.create_task(resilience.resilience_loop())
    app.state.ap_status_task = asyncio.create_task(ap_status_loop())
    app.state.wan_refresh_task = asyncio.create_task(wan_refresh_loop())
    app.state.wan_bootstrap_task = asyncio.create_task(bootstrap_wan())
    if await sla.needs_backfill():
        # Seed an empty database, or top up devices added after the first backfill
        # (force=False resumes each device from its last covered day, so this is cheap
        # for devices already up to date and full-history only for newcomers).
        app.state.sla_backfill_task = asyncio.create_task(sla.backfill_all())


@app.on_event("shutdown")
async def shutdown():
    for name in ("switch_refresh_task", "sla_rollup_task", "resilience_task", "ap_status_task", "wan_refresh_task", "wan_bootstrap_task", "sla_backfill_task"):
        task = getattr(app.state, name, None)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@app.middleware("http")
async def secure_headers(request: Request, call_next):
    request.state.request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    response = await call_next(request)
    response.headers.update({"X-Content-Type-Options":"nosniff", "X-Frame-Options":"DENY", "Referrer-Policy":"no-referrer", "Permissions-Policy":"camera=(), microphone=(), geolocation=()", "Content-Security-Policy":"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'", "Cross-Origin-Opener-Policy":"same-origin", "Cross-Origin-Resource-Policy":"same-origin", "Strict-Transport-Security":"max-age=63072000; includeSubDomains", "X-Request-ID":request.state.request_id})
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


@app.get("/api/v1/readiness")
def readiness(user=Depends(administrator)):
    """Admin-only: production-configuration readiness — lists blocking issues (default
    credentials, localhost CORS, SQLite, etc.) so ops can confirm before go-live."""
    issues = settings.production_issues()
    return {"environment": settings.environment, "is_production": settings.is_production,
            "ready": not issues, "issues": issues}


@app.get("/api/v1/dashboard")
def dashboard(user=Depends(current_user), db: Session = Depends(session)):
    devices = db.scalars(select(Device)).all(); counts = defaultdict(int)
    for d in devices: counts["active" if d.active else "inactive"] += 1; counts[d.device_type] += 1; counts[d.match_status] += 1
    fleet = sla.fleet_sla(db); fy = fleet["fleet_ytd"]
    return {"devices":len(devices), "counts":counts, "sla":{"actual":fy["availability"],"target":settings.sla_target,"coverage":fy["coverage"],"status":fy["status"],"as_of":fleet["as_of"]}, "last_collection":None}


@app.get("/api/v1/devices", response_model=list[DeviceOut])
def devices(user=Depends(current_user), db: Session = Depends(session)): return db.scalars(select(Device).order_by(Device.hostname)).all()


DETAIL_TABLES = {
    "Health Data": ["Collected", "Status", "Score", "Model", "OS", "Serial", "MAC", "Uptime", "Reload Reason", "Restart Time", "CPU 5s %", "CPU 1m %", "CPU 5m %", "Memory Total", "Memory Used", "Memory %", "Max Temp C", "PoE Available W", "PoE Used W", "PoE Ports On"],
    "Ping Quality": ["24h Availability", "7d Availability", "Loss %", "Min Latency", "Avg Latency", "Max Latency", "P95 Latency", "Jitter", "Probe Count", "Trend"],
    "Interfaces": ["Interface", "Description", "Status", "VLAN", "Duplex", "Speed", "Type", "Align Errors", "FCS/CRC Errors", "TX Errors", "RX Errors", "Undersize", "Out Discards"],
    "VLANs": ["VLAN ID", "Name", "Status", "Ports"],
    "CDP-LLDP Neighbors": ["Protocol", "Local Interface", "Neighbor", "Management IP", "Platform/Model", "Remote Port"],
    "OSPF Neighbors": ["Neighbor", "State", "Neighbor Events", "Restarts", "Retransmit Queue"],
    "Inventory": ["Component", "Description", "PID", "VID", "Serial Number"],
    "Spanning Tree": ["Instance", "Blocking", "Listening", "Learning", "Forwarding", "Active"],
    "Environmental and PoE": ["Category", "Switch/Module", "Component/Interface", "State", "Temperature C", "Fan RPM", "Watts", "Details"],
    "Alerts": ["Severity", "Source", "Instance", "Message", "Age", "Acknowledged"],
    "Configuration Backups": ["Running Config", "Running Saved", "Startup Config", "Startup Saved", "Collected"],
    "Monitoring Gaps": ["Metric", "Reason", "Recommended Action"],
    "Collection Details": ["DataSource", "Instance", "Success", "Latest Data", "Error"],
}


_IF_LONG = (("tengigabitethernet", "te"), ("fortygigabitethernet", "fo"), ("twentyfivegige", "twe"),
            ("gigabitethernet", "gi"), ("fastethernet", "fa"), ("ethernet", "et"), ("port-channel", "po"),
            ("portchannel", "po"), ("loopback", "lo"), ("tunnel", "tu"), ("vlan", "vl"), ("management", "ma"))


def _ifkey(name):
    """Canonical interface key so 'GigabitEthernet1/0/1' and 'Gi1/0/1' match on merge."""
    s = re.sub(r"\s+", "", str(name or "")).lower()
    for long, short in _IF_LONG:
        if s.startswith(long):
            return short + s[len(long):]
    return s


# SSH show-command output that is authoritative and should replace the LM display table
# (LM cannot provide these columns at all, e.g. inventory PID/VID/serial).
_SSH_AUTHORITATIVE = {"Inventory"}


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
            rows = [{"Metric": x, "Reason": "No applied LogicMonitor DataSource mapping", "Recommended Action": "Admin can pull directly via read-only SSH"} for x in missing]
        tables.append({"name": name, "columns": columns, "rows": rows, "evidence_status": "Available" if rows else ("Collection failed" if item.match_status == "Collection failed" else "Not available from LogicMonitor")})

    # Overlay any admin-pulled SSH facts (kept until the next SSH push) onto the tables.
    facts = db.scalar(select(SshFacts).where(SshFacts.device_id == device_id))
    ssh_meta = None
    if facts and facts.data:
        ssh_tables = facts.data.get("tables", {})
        by_name = {t["name"]: t for t in tables}
        # Enrich interface rows (VLAN / duplex / speed / errors) by normalized interface name.
        if_status = ssh_tables.get("_if_status", {})
        if if_status and by_name.get("Interfaces", {}).get("rows"):
            if_norm = {_ifkey(k): v for k, v in if_status.items()}
            for row in by_name["Interfaces"]["rows"]:
                enr = if_norm.get(_ifkey(row.get("Interface")))
                if enr:
                    for col in ("Description", "Status", "VLAN", "Duplex", "Speed", "Type", "FCS/CRC Errors", "Align Errors"):
                        cur = row.get(col)
                        if enr.get(col) and (cur is None or str(cur).strip() == "" or str(cur).lower().startswith("not ")):
                            row[col] = enr[col]
        # Overlay empty LM tables, replace authoritative ones, and append SSH-only tables.
        for name, rows in ssh_tables.items():
            if name == "_if_status":
                continue
            if name in by_name:
                if not by_name[name]["rows"] or name in _SSH_AUTHORITATIVE:
                    by_name[name]["rows"] = rows
                    by_name[name]["evidence_status"] = "Collected via SSH"
                    by_name[name]["source"] = "ssh"
            else:
                tables.append({"name": name, "columns": list(rows[0].keys()) if rows else [], "rows": rows,
                               "evidence_status": "Manually collected", "source": "ssh"})
        # Drop monitoring-gap rows that SSH has since filled.
        gaps = by_name.get("Monitoring Gaps")
        if gaps:
            gaps["rows"] = [g for g in gaps["rows"] if not (by_name.get(g.get("Metric"), {}) or {}).get("rows")]
        ssh_meta = {"collected_at": facts.collected_at.isoformat() if facts.collected_at else None,
                    "collected_by": facts.collected_by, "status": facts.status, "has_config": bool(facts.data.get("config"))}
    return {"device": DeviceOut.model_validate(item), "tables": tables, "source": "LogicMonitor read-only API", "ssh": ssh_meta}


class ManualCollect(BaseModel):
    transcript: str = Field(max_length=5_000_000)  # cap pasted output (~5 MB) to prevent abuse


@app.get("/api/v1/devices/{device_id}/collect-plan")
def collect_plan(device_id: int, user=Depends(administrator), db: Session = Depends(session)):
    """Admin-only: the read-only `show` commands to run manually on this device, plus which
    detail tables are currently gaps (empty in LogicMonitor). The account has MFA, so the
    admin runs these in their own SSH session and pastes the output back."""
    item = db.get(Device, device_id)
    if not item:
        raise HTTPException(404, "Device not found")
    latest = db.scalar(select(Snapshot).where(Snapshot.device_id == device_id).order_by(Snapshot.collected_at.desc()).limit(1))
    have = (latest.details.get("tables", {}) if latest and latest.details else {})
    gap_tables = ("VLANs", "Spanning Tree", "CDP-LLDP Neighbors", "Inventory", "OSPF Neighbors", "Environmental and PoE")
    gaps = [t for t in gap_tables if not have.get(t)]
    return {"hostname": item.hostname, "management_ip": item.management_ip, "username": "pa-phessamfar",
            "script": ssh_collect.SCRIPT, "commands": ssh_collect.COMMANDS, "gaps": gaps, "gap_commands": ssh_collect.GAP_COMMANDS}


@app.post("/api/v1/devices/{device_id}/manual-collect")
def manual_collect(device_id: int, body: ManualCollect, user=Depends(administrator), db: Session = Depends(session)):
    """Admin-only: parse an admin-pasted read-only `show` transcript and store it as an
    overlay that fills the LogicMonitor gaps until the next manual collection. The
    application never connects to the device and never handles any credential."""
    item = db.get(Device, device_id)
    if not item:
        raise HTTPException(404, "Device not found")
    result = ssh_collect.parse_transcript(body.transcript)
    outcome = result.get("status")
    db.add(AuditEvent(actor=user.get("sub", "admin"), action="device.manual_collect", target=f"device:{device_id}",
                      details={"result": outcome, "recognised": (result.get("data") or {}).get("recognised")}))
    if outcome != "ok":
        db.commit()
        return {"status": outcome, "message": result.get("message")}
    facts = db.scalar(select(SshFacts).where(SshFacts.device_id == device_id)) or SshFacts(device_id=device_id)
    facts.host = item.management_ip or ""; facts.status = "ok"; facts.collected_by = user.get("sub", "admin")
    facts.collected_at = datetime.now(timezone.utc); facts.data = result["data"]
    db.add(facts); db.commit()
    return {"status": "ok", "collected_at": facts.collected_at.isoformat(), "collected_by": facts.collected_by,
            "tables": {k: v for k, v in result["data"]["tables"].items() if k != "_if_status"},
            "recognised": result["data"].get("recognised"), "has_config": bool(result["data"].get("config"))}


class SshRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


@app.post("/api/v1/devices/{device_id}/ssh-collect")
def ssh_collect_device(device_id: int, body: SshRequest, user=Depends(administrator), db: Session = Depends(session)):
    """Admin-only: connect over READ-ONLY SSH and fill the LogicMonitor gaps. Username is
    fixed to pa-phessamfar; the target is the device Management IP. The call blocks while the
    device's auth backend waits for the administrator's push-MFA (Microsoft Authenticator)
    approval, then runs the fixed `show`-command allow-list. The password is used for one
    session and never stored or logged."""
    item = db.get(Device, device_id)
    if not item:
        raise HTTPException(404, "Device not found")
    host = item.management_ip
    if not host:
        return {"status": "no_ip", "message": "This device has no Management IP on record, so SSH cannot be attempted."}
    result = ssh_collect.collect(host, "pa-phessamfar", body.password)
    outcome = result.get("status")
    db.add(AuditEvent(actor=user.get("sub", "admin"), action="device.ssh_collect", target=f"device:{device_id}",
                      details={"host": host, "result": outcome}))  # never logs the password
    if outcome != "ok":
        db.commit()
        return {"status": outcome, "message": result.get("message")}
    facts = db.scalar(select(SshFacts).where(SshFacts.device_id == device_id)) or SshFacts(device_id=device_id)
    facts.host = host; facts.status = "ok"; facts.collected_by = user.get("sub", "admin")
    facts.collected_at = datetime.now(timezone.utc); facts.data = result["data"]
    db.add(facts); db.commit()
    return {"status": "ok", "collected_at": facts.collected_at.isoformat(), "collected_by": facts.collected_by,
            "tables": {k: v for k, v in result["data"]["tables"].items() if k != "_if_status"},
            "recognised": result["data"].get("recognised"), "has_config": bool(result["data"].get("config"))}


@app.get("/api/v1/devices/{device_id}/ssh-config")
def ssh_config(device_id: int, user=Depends(administrator), db: Session = Depends(session)):
    """Admin-only: the running-config text captured by the last manual collection (read-only)."""
    facts = db.scalar(select(SshFacts).where(SshFacts.device_id == device_id))
    cfg = (facts.data or {}).get("config") if facts else None
    if not cfg:
        raise HTTPException(404, "No manually-collected running-config available")
    return {"config": cfg, "collected_at": facts.collected_at.isoformat() if facts.collected_at else None}


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


@app.post("/api/v1/reports/executive/excel")
def report_executive_excel(user=Depends(current_user), db:Session=Depends(session)):
    path=reports.executive_excel(db); audit(db,user["sub"],"report.executive_excel",path.name); db.commit()
    return {"filename":path.name,"download_url":f"/api/v1/reports/{path.name}"}


@app.post("/api/v1/reports/executive/pptx")
def report_executive_pptx(user=Depends(current_user), db:Session=Depends(session)):
    path=reports.executive_pptx(db); audit(db,user["sub"],"report.executive_pptx",path.name); db.commit()
    return {"filename":path.name,"download_url":f"/api/v1/reports/{path.name}"}


_help_cache = {"data": None, "at": 0.0}


@app.get("/api/v1/help/explain")
def help_explain(user=Depends(current_user), db: Session = Depends(session)):
    """Live figures for the Help Center's 'why this number?' explanations. Reuses the EXACT
    same analytics builders as the dashboards (overview/telemetry/trends) so Help can never
    disagree with what the pages show — one source of truth."""
    now = time.time()
    if _help_cache["data"] is None or now - _help_cache["at"] > 60:
        o = overview.build(db); g = o["global_sla"]; h = o["header"]; crit = o["criticality"]
        tel = telemetry.build(db); tr = trends.build(db)
        avail_sites = [s for s in o["sites"] if isinstance(s.get("availability_ytd"), (int, float))]
        worst = min(avail_sites, key=lambda s: s["availability_ytd"], default=None)
        lat = tel["latency"]; iface = tel["interfaces"]; rt = tel["routing"]
        _help_cache["data"] = {
            "global_sla": {"current": g["current"], "target": g["target"], "delta": g["delta"], "status": g["status"],
                            "ytd": g["ytd"], "coverage": g["coverage"], "trend_7d": g["trend_7d"], "trend_30d": g["trend_30d"]},
            "fleet": {"total": h["fleet_coverage"]["total"], "monitored": h["fleet_coverage"]["monitored"],
                       "matched": h["lm_coverage"]["matched"], "evidence_coverage": h["sla_evidence_coverage"], "data_quality": h["data_quality"]},
            "criticality": {"total": crit["total"], "bands": crit["bands"], "degraded": crit["degraded"], "unreachable": crit["unreachable"]},
            "business_units": [{"name": b["business_unit"], "ytd": b["availability_ytd"], "d30": b["availability_30d"],
                                 "devices": b["devices"], "sites": b["sites"], "incidents": b["incidents"], "status": b["status"], "reason": b.get("reason")} for b in o.get("business_units", [])],
            "worst_site": ({"site": worst["site_code"], "city": worst["city"], "ytd": worst["availability_ytd"]} if worst else None),
            "incidents": {"count": tr["mttr_mtbf"]["incidents"], "window_days": tr["mttr_mtbf"]["window_days"],
                           "mttr_minutes": tr["mttr_mtbf"]["mttr_minutes"], "mtbf_hours": tr["mttr_mtbf"]["mtbf_hours"]},
            "trends": {"wow": tr["deltas"]["wow"]["trend"], "mom": tr["deltas"]["mom"]["trend"]},
            "telemetry": {"latency_avg": lat["avg_latency_ms"], "latency_max": lat["max_latency_ms"], "loss_avg": lat["avg_loss_pct"],
                           "if_total": iface["total"], "if_up": iface["up"], "if_down": iface["down"], "if_high": iface["high_util"], "if_errors": iface["with_errors"],
                           "ospf_devices": rt["devices_with_ospf"], "ospf_fleet": rt["fleet"]},
            "resilience": {"tier": o["resilience"]["fleet_tier"]},
            "target": g["target"], "coverage_threshold": settings.coverage_threshold,
        }
        _help_cache["at"] = now
    return _help_cache["data"]


@app.get("/api/v1/reports/{filename}")
def download(filename:str, user=Depends(current_user)):
    if not re.fullmatch(r"(Network_Health|Executive_Reliability)_[0-9_-]+\.(xlsx|pptx)",filename): raise HTTPException(400,"Invalid report name")
    path=Path(settings.report_dir)/filename
    if not path.is_file(): raise HTTPException(404,"Report not found")
    media="application/vnd.openxmlformats-officedocument.presentationml.presentation" if filename.endswith(".pptx") else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return FileResponse(path,filename=filename,media_type=media)


@app.get("/api/v1/settings")
def app_settings(user=Depends(administrator)):
    return {"sla_target":settings.sla_target,"coverage_threshold":settings.coverage_threshold,"stale_minutes":settings.stale_minutes,"switch_collection_interval_minutes":settings.switch_collection_interval_minutes,"logicmonitor_portal":settings.lm_portal_url,"credentials_configured":bool(settings.access_id and settings.access_key)}


@app.get("/api/v1/devices/{device_id}/sla")
def device_sla_endpoint(device_id:int, user=Depends(current_user), db:Session=Depends(session)):
    if not db.get(Device, device_id): raise HTTPException(404,"Device not found")
    return {"device_id":device_id,"timezone":settings.sla_timezone,"target":settings.sla_target,"windows":sla.device_sla(db, device_id)}


_overview_cache = {"at": 0.0, "data": None}


@app.get("/api/v1/overview")
def overview_endpoint(user=Depends(current_user), db:Session=Depends(session)):
    now = time.time()
    if _overview_cache["data"] is None or now - _overview_cache["at"] > 30:
        _overview_cache["data"] = overview.build(db); _overview_cache["at"] = now
    return _overview_cache["data"]


@app.get("/api/v1/telemetry")
def telemetry_endpoint(user=Depends(current_user), db:Session=Depends(session)):
    return telemetry.build(db)


@app.get("/api/v1/trends")
def trends_endpoint(user=Depends(current_user), db:Session=Depends(session)):
    return trends.build(db)


@app.get("/api/v1/sla/summary")
def sla_summary(user=Depends(current_user), db:Session=Depends(session)):
    summary = sla.fleet_sla(db); summary["resilience"] = resilience.latest_assessment(db); return summary


@app.get("/api/v1/sla/resilience")
def sla_resilience(user=Depends(current_user), db:Session=Depends(session)):
    return resilience.latest_assessment(db)


# Manual backfill runs in the background (it can take minutes across the fleet) so the HTTP
# request returns immediately and the UI polls for status instead of timing out at the proxy.
_backfill_state = {"running": False, "result": None, "error": None, "started_at": None, "finished_at": None}


async def _run_backfill_job(actor: str):
    _backfill_state.update(running=True, result=None, error=None,
                           started_at=datetime.now(timezone.utc).isoformat(), finished_at=None)
    try:
        result = await sla.backfill_all(force=False)
        await resilience.run_assessment()
        with SessionLocal() as db:
            audit(db, actor, "sla.backfill", "LogicMonitor", {"devices": result["devices"]}); db.commit()
        _backfill_state["result"] = result
    except Exception as exc:
        _backfill_state["error"] = type(exc).__name__
        logging.getLogger("backfill").exception("Manual backfill failed")
    finally:
        _backfill_state["running"] = False
        _backfill_state["finished_at"] = datetime.now(timezone.utc).isoformat()


@app.post("/api/v1/sla/backfill")
async def sla_backfill(user=Depends(administrator)):
    """Admin-only: start a fleet SLA backfill + resilience reassessment in the background.
    Returns immediately; poll GET /sla/backfill/status for progress."""
    if _backfill_state["running"]:
        return {"status": "already_running", "started_at": _backfill_state["started_at"]}
    asyncio.create_task(_run_backfill_job(user["sub"]))
    return {"status": "started"}


@app.get("/api/v1/sla/backfill/status")
def sla_backfill_status(user=Depends(administrator)):
    return _backfill_state
