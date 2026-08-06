from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

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


engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def session():
    with SessionLocal() as db:
        yield db

