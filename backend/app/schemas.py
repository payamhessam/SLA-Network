"""Pydantic request/response schemas for the device API.

Defines the validated shapes for creating/updating devices and returning device
data. Field constraints here (hostname pattern, length limits, enum literals, IP
validation) are the first line of input validation before anything reaches the ORM.
"""
from ipaddress import ip_address
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


DeviceType = Literal["switch", "router", "access_point"]


class DeviceCreate(BaseModel):
    hostname: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9._-]+$")
    management_ip: str | None = None
    site: str = Field(default="Unassigned", max_length=120)
    role: str = Field(default="Access", max_length=80)
    criticality: Literal["Low", "Medium", "High", "Critical"] = "Medium"
    device_type: DeviceType = "switch"
    model: str | None = Field(default=None, max_length=100)
    active: bool = True
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("management_ip")
    @classmethod
    def valid_ip(cls, value):
        if value: ip_address(value)
        return value


class DeviceUpdate(DeviceCreate): pass


class DeviceOut(DeviceCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    lm_device_id: int | None
    match_status: str


class Login(BaseModel):
    username: str
    password: str

