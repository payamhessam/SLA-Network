"""Business application monitoring based on local reachability and read-only LogicMonitor evidence."""
import asyncio
import json
import logging
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import current_user
from .collection import latest, numeric
from .config import get_settings
from .db import ApplicationObservation, ApplicationService, SessionLocal, session
from .logicmonitor import LogicMonitorClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/applications", tags=["applications"])

def _configured_scope() -> list[dict]:
    """Read the company-specific application scope from its local Docker data mount.

    The configuration is deliberately not part of source control: application names and
    internal addresses are operational data, not program code. SAP rows may omit a port
    until the approved business transaction is known, avoiding a guessed false failure.
    """
    path = Path(get_settings().application_services_file)
    if not path.exists():
        logger.warning("Application scope file is not present: %s", path)
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError("scope must be a JSON list")
        required = {"service_key", "service_name", "application", "environment", "endpoint", "endpoint_kind"}
        return [row for row in rows if isinstance(row, dict) and required <= row.keys()]
    except (OSError, ValueError, json.JSONDecodeError):
        logger.exception("Application scope file is invalid")
        return []


def bootstrap_applications(db: Session) -> None:
    existing = set(db.scalars(select(ApplicationService.service_key)).all())
    for row in _configured_scope():
        if row["service_key"] not in existing:
            db.add(ApplicationService(service_key=row["service_key"], service_name=row["service_name"], application=row["application"],
                                      environment=row["environment"], endpoint=row["endpoint"], endpoint_kind=row["endpoint_kind"],
                                      check_port=row.get("check_port"), criticality=row.get("criticality", "High")))
    db.commit()


async def _port_probe(host: str, port: int) -> tuple[bool, float | None]:
    started = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=6)
        writer.close(); await writer.wait_closed()
        return True, round((time.perf_counter() - started) * 1000, 1)
    except (OSError, asyncio.TimeoutError):
        return False, None


async def _dns_probe(host: str) -> bool:
    try:
        await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM), timeout=6)
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def _lm_ping(client: LogicMonitorClient, service: ApplicationService) -> tuple[dict, int | None, str | None, str]:
    """Use only the standard Ping datasource. Missing/ambiguous mapping is honest evidence."""
    hostname = service.endpoint if service.endpoint_kind == "hostname" else service.service_name.split(" · ")[0]
    remote, method = await client.find_device(hostname, service.endpoint if service.endpoint_kind == "ip" else None)
    if not remote:
        return {}, None, None, "Mapping pending" if method in ("Not Found", "Ambiguous") else method
    device_id = int(remote["id"])
    applied = await client.applied_datasources(device_id)
    by_name = {(x.get("dataSourceName") or x.get("name")): x for x in applied}

    async def latest_for(name: str) -> dict:
        source = by_name.get(name)
        if not source:
            return {}
        instances = await client.instances(device_id, int(source["id"]))
        if not instances:
            return {}
        data = await client.instance_data(device_id, int(source["id"]), int(instances[0]["id"]), int(time.time()) - 3600, int(time.time()))
        return latest(data)

    ping_row, linux_row, snmp_memory_row = await asyncio.gather(latest_for("Ping"), latest_for("Linux_SSH_CPUMemory"), latest_for("NetSNMP_Memory_Usage"))
    if not ping_row:
        return {"cpu": numeric(linux_row.get("CPUBusyPercent")), "memory": numeric(linux_row.get("PercentUsedMemory")),
                "host_status": remote.get("hostStatus")}, device_id, remote.get("displayName"), "Matched · Ping not monitored"
    memory = numeric(linux_row.get("PercentUsedMemory"))
    if memory is None:
        used, total = numeric(snmp_memory_row.get("UsedMemory")), numeric(snmp_memory_row.get("TotalReal"))
        memory = round(used * 100 / total, 1) if used is not None and total else None
    return {"loss": numeric(ping_row.get("PingLossPercent")), "average": numeric(ping_row.get("average")), "max": numeric(ping_row.get("maxrtt")),
            "cpu": numeric(linux_row.get("CPUBusyPercent")), "memory": memory, "host_status": remote.get("hostStatus")}, device_id, remote.get("displayName"), "Matched"


async def collect_application(service: ApplicationService) -> dict:
    dns_ok = await _dns_probe(service.endpoint) if service.endpoint_kind == "hostname" else None
    port_ok, direct_latency = (await _port_probe(service.endpoint, service.check_port)) if service.check_port and (dns_ok is not False) else (None, None)
    lm, lm_id, lm_name, mapping = {}, None, None, "LogicMonitor not configured"
    settings = get_settings()
    if settings.lm_portal_url and settings.access_id and settings.access_key:
        try:
            lm, lm_id, lm_name, mapping = await _lm_ping(LogicMonitorClient(), service)
        except Exception:
            logger.exception("LogicMonitor application collection failed for %s", service.service_key)
            mapping = "LogicMonitor collection failed"
    loss = lm.get("loss")
    if port_ok is False or dns_ok is False:
        status = "Critical"
    elif loss is not None and loss >= 20:
        status = "Critical"
    elif loss is not None and loss >= 2:
        status = "Warning"
    elif port_ok is True or (loss is not None and loss < 2):
        status = "Healthy"
    else:
        status = "Unknown"
    return {"dns_ok": dns_ok, "port_ok": port_ok, "latency_ms": direct_latency if direct_latency is not None else lm.get("average"),
            "packet_loss_pct": loss, "availability_pct": None, "status": status, "mapping": mapping,
            "lm_id": lm_id, "lm_name": lm_name, "evidence": {"ping_max_ms": lm.get("max"), "cpu_pct": lm.get("cpu"), "memory_pct": lm.get("memory"),
            "host_status": lm.get("host_status"), "check": "HTTPS reachability" if service.check_port else "LogicMonitor network quality"}}


async def refresh_applications_once() -> None:
    with SessionLocal() as db:
        services = db.scalars(select(ApplicationService).where(ApplicationService.enabled.is_(True))).all()
        for service in services:
            result = await collect_application(service)
            service.logicmonitor_device_id = result["lm_id"] or service.logicmonitor_device_id
            service.logicmonitor_display_name = result["lm_name"] or service.logicmonitor_display_name
            service.mapping_status = result["mapping"]
            service.mapping_checked_at = datetime.now(timezone.utc)
            db.add(ApplicationObservation(application_service_id=service.id, collected_at=datetime.now(timezone.utc), status=result["status"],
                                          dns_ok=result["dns_ok"], port_ok=result["port_ok"], latency_ms=result["latency_ms"],
                                          packet_loss_pct=result["packet_loss_pct"], availability_pct=result["availability_pct"],
                                          route_status="Route evidence pending", evidence=result["evidence"]))
        db.commit()


async def application_refresh_loop() -> None:
    while True:
        try:
            await refresh_applications_once()
        except Exception:
            logger.exception("Application refresh loop failed")
        await asyncio.sleep(max(60, get_settings().application_collection_interval_minutes * 60))


@router.get("")
def applications(db: Session = Depends(session), user=Depends(current_user)):
    services = db.scalars(select(ApplicationService).order_by(ApplicationService.application, ApplicationService.service_name)).all()
    output = []
    for service in services:
        latest_observation = db.scalars(select(ApplicationObservation).where(ApplicationObservation.application_service_id == service.id).order_by(ApplicationObservation.collected_at.desc()).limit(1)).first()
        output.append({"id": service.id, "name": service.service_name, "application": service.application, "environment": service.environment,
                       "endpoint": service.endpoint, "criticality": service.criticality, "mapping": service.mapping_status,
                       "logicmonitor": service.logicmonitor_display_name, "last_checked": latest_observation.collected_at if latest_observation else None,
                       "status": latest_observation.status if latest_observation else "Baseline pending", "latency_ms": latest_observation.latency_ms if latest_observation else None,
                       "packet_loss_pct": latest_observation.packet_loss_pct if latest_observation else None,
                       "cpu_pct": latest_observation.evidence.get("cpu_pct") if latest_observation else None,
                       "memory_pct": latest_observation.evidence.get("memory_pct") if latest_observation else None,
                       "dns_ok": latest_observation.dns_ok if latest_observation else None, "port_ok": latest_observation.port_ok if latest_observation else None,
                       "route_status": latest_observation.route_status if latest_observation else "Route evidence pending"})
    return {"items": output, "total": len(output)}
