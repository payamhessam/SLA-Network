"""Database engine, session factory, and SQLAlchemy ORM models.

Defines every persistent table: users, the local Device Fleet inventory (sites,
zones, device types, InventoryDevice) and its legacy Device/Snapshot collection
store, the daily SLA rollup (SlaDaily), resilience assessments, access points, and
audit events. Also builds the engine and SessionLocal factory with a connection pool
sized for the concurrent background collectors.
"""
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import get_settings


class Base(DeclarativeBase): pass


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[int] = mapped_column(primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    management_ip: Mapped[str | None] = mapped_column(String(45), index=True)
    site: Mapped[str] = mapped_column(String(120), default="Unassigned")
    role: Mapped[str] = mapped_column(String(80), default="Access")
    criticality: Mapped[str] = mapped_column(String(30), default="Medium")
    device_type: Mapped[str] = mapped_column(String(30), default="switch")
    model: Mapped[str | None] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(String(1000))
    lm_device_id: Mapped[int | None] = mapped_column(Integer)
    match_status: Mapped[str] = mapped_column(String(30), default="Mapping pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    snapshots: Mapped[list["Snapshot"]] = relationship(cascade="all, delete-orphan")


class Snapshot(Base):
    __tablename__ = "device_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    status: Mapped[str] = mapped_column(String(30), default="Unknown")
    availability: Mapped[float | None] = mapped_column(Float)
    cpu: Mapped[float | None] = mapped_column(Float)
    memory: Mapped[float | None] = mapped_column(Float)
    temperature: Mapped[float | None] = mapped_column(Float)
    ap_clients: Mapped[int | None] = mapped_column(Integer)
    radio_utilization: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    actor: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(120))
    target: Mapped[str] = mapped_column(String(255))
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class Site(Base):
    __tablename__ = "sites"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    city: Mapped[str] = mapped_column(String(120))
    province_region: Mapped[str] = mapped_column(String(80), default="")
    country: Mapped[str] = mapped_column(String(80), default="Canada")
    business_unit: Mapped[str] = mapped_column(String(40), default="Unassigned")
    description: Mapped[str | None] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    created_by: Mapped[str] = mapped_column(String(120), default="system")
    updated_by: Mapped[str] = mapped_column(String(120), default="system")


class Zone(Base):
    __tablename__ = "zones"
    id: Mapped[int] = mapped_column(primary_key=True)
    zone_code: Mapped[str] = mapped_column(String(10), unique=True)
    description: Mapped[str] = mapped_column(String(120))
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class InventoryDeviceType(Base):
    __tablename__ = "inventory_device_types"
    id: Mapped[int] = mapped_column(primary_key=True)
    type_code: Mapped[str] = mapped_column(String(10), unique=True)
    description: Mapped[str] = mapped_column(String(120))
    derived_role: Mapped[str] = mapped_column(String(80))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)


class InventoryDevice(Base):
    __tablename__ = "inventory_devices"
    __table_args__ = (UniqueConstraint("site_id", "zone_id", "device_type_id", "device_number", name="uq_inventory_name_parts"), CheckConstraint("length(device_number) = 2", name="ck_device_number_two_digits"))
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="RESTRICT"), index=True)
    zone_id: Mapped[int] = mapped_column(ForeignKey("zones.id", ondelete="RESTRICT"))
    device_type_id: Mapped[int] = mapped_column(ForeignKey("inventory_device_types.id", ondelete="RESTRICT"))
    device_number: Mapped[str] = mapped_column(String(2))
    generated_name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(80))
    criticality: Mapped[str] = mapped_column(String(30), default="Medium")
    management_ip: Mapped[str | None] = mapped_column(String(45), unique=True)
    model: Mapped[str | None] = mapped_column(String(100))
    manufacturer: Mapped[str | None] = mapped_column(String(100))
    os_version: Mapped[str | None] = mapped_column(String(120))
    logicmonitor_device_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    logicmonitor_display_name: Mapped[str | None] = mapped_column(String(255))
    logicmonitor_match_status: Mapped[str] = mapped_column(String(50), default="Pending LogicMonitor Match")
    logicmonitor_match_confidence: Mapped[str | None] = mapped_column(String(30))
    logicmonitor_groups: Mapped[list] = mapped_column(JSON, default=list)
    critical_interfaces: Mapped[list] = mapped_column(JSON, default=list)
    ignored_interfaces: Mapped[list] = mapped_column(JSON, default=list)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(String(1000))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_logicmonitor_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    created_by: Mapped[str] = mapped_column(String(120), default="system")
    updated_by: Mapped[str] = mapped_column(String(120), default="system")
    site: Mapped[Site] = relationship()
    zone: Mapped[Zone] = relationship()
    device_type: Mapped[InventoryDeviceType] = relationship()


class ImportJob(Base):
    __tablename__ = "inventory_import_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor: Mapped[str] = mapped_column(String(120))
    mode: Mapped[str] = mapped_column(String(30), default="valid_rows_only")
    status: Mapped[str] = mapped_column(String(30), default="Validated")
    rows: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class AccessPointInventory(Base):
    __tablename__ = "access_point_inventory"
    id: Mapped[int] = mapped_column(primary_key=True)
    ap_name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    site_code: Mapped[str] = mapped_column(String(20), index=True)
    city: Mapped[str | None] = mapped_column(String(120))
    province: Mapped[str | None] = mapped_column(String(80))
    country: Mapped[str | None] = mapped_column(String(80))
    ap_model: Mapped[str | None] = mapped_column(String(120), index=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), index=True)
    ethernet_mac: Mapped[str | None] = mapped_column(String(17), index=True)
    radio_mac: Mapped[str | None] = mapped_column(String(17), index=True)
    serial_number: Mapped[str | None] = mapped_column(String(120), index=True)
    import_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    imported_by: Mapped[str] = mapped_column(String(120))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    status: Mapped[str] = mapped_column(String(20), default="Offline")
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connected_switch: Mapped[str | None] = mapped_column(String(255))
    connected_interface: Mapped[str | None] = mapped_column(String(255))
    last_status_check: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccessPointImport(Base):
    __tablename__ = "access_point_imports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    imported_by: Mapped[str] = mapped_column(String(120))
    file_name: Mapped[str] = mapped_column(String(255))
    file_checksum: Mapped[str] = mapped_column(String(64))
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    validation_errors: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    import_mode: Mapped[str] = mapped_column(String(20), default="replace")
    status: Mapped[str] = mapped_column(String(30), default="Validated")
    rows: Mapped[list] = mapped_column(JSON, default=list)


class SlaDaily(Base):
    __tablename__ = "sla_daily"
    __table_args__ = (UniqueConstraint("device_id", "day", name="uq_sla_daily_device_day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    day: Mapped[Date] = mapped_column(Date, index=True)
    expected_minutes: Mapped[int] = mapped_column(Integer, default=0)
    observed_minutes: Mapped[int] = mapped_column(Integer, default=0)
    up_minutes: Mapped[int] = mapped_column(Integer, default=0)
    availability: Mapped[float | None] = mapped_column(Float)
    coverage: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(120), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class ResilienceAssessment(Base):
    __tablename__ = "resilience_assessments"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    scope: Mapped[str] = mapped_column(String(20), default="fleet")
    device_id: Mapped[int | None] = mapped_column(Integer, index=True)
    tier: Mapped[str] = mapped_column(String(30), default="Insufficient")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    availability_wtd: Mapped[float | None] = mapped_column(Float)
    availability_ytd: Mapped[float | None] = mapped_column(Float)
    signals: Mapped[dict] = mapped_column(JSON, default=dict)
    rationale: Mapped[list] = mapped_column(JSON, default=list)


class WanRouter(Base):
    """WAN provider / carrier-edge routers (e.g. Centrilogic-managed circuits).

    These devices belong to the WAN providers, NOT to Medline. They are collected
    read-only from LogicMonitor for visibility only and are deliberately kept in
    their own table so they can NEVER be joined into the Device Fleet analytics
    (SLA, Overview, resilience, trends, reports). The latest collected snapshot is
    stored inline on the row (status/details JSON) — no history, since nothing about
    these routers is ever charted or scored for the company."""
    __tablename__ = "wan_routers"
    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(120), default="")
    site_label: Mapped[str] = mapped_column(String(180), default="")
    management_ip: Mapped[str | None] = mapped_column(String(64))
    lm_device_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    match_status: Mapped[str] = mapped_column(String(40), default="Pending")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(40), default="Unknown")
    cpu: Mapped[float | None] = mapped_column(Float)
    memory: Mapped[float | None] = mapped_column(Float)
    temperature: Mapped[float | None] = mapped_column(Float)
    uptime: Mapped[float | None] = mapped_column(Float)
    reachability: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict | None] = mapped_column(JSON)
    last_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    created_by: Mapped[str] = mapped_column(String(120), default="")


database_url = get_settings().database_url
engine = create_engine(database_url, pool_pre_ping=True, **({"connect_args":{"check_same_thread":False},"poolclass":StaticPool} if database_url.startswith("sqlite") else {"pool_size":12,"max_overflow":24,"pool_timeout":30}))
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def session():
    with SessionLocal() as db:
        yield db
