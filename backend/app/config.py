"""Application settings, loaded from environment / Docker secret files.

Defines the typed Settings model (database URL, LogicMonitor portal + credentials,
SLA target/timezone/coverage threshold, JWT secret, collection intervals, etc.) and
`get_settings()`, an lru_cached accessor used everywhere. In production the sensitive
values are read from mounted Docker secret files rather than the repository, so the
defaults here are safe placeholders only.
"""
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:////data/network_sla.db"
    lm_portal_url: str = ""
    lm_access_id: str = ""
    lm_access_key: str = ""
    lm_access_id_file: str = ""
    lm_access_key_file: str = ""
    jwt_secret: str = "development-only-change-me"
    jwt_secret_file: str = ""
    local_admin_password: str = "change-this-development-password"
    local_admin_password_file: str = ""
    local_user_password: str = "change-this-viewer-password"
    local_user_password_file: str = ""
    allowed_origins: str = "http://localhost:8080"
    report_dir: str = "/reports"
    stale_minutes: int = 30
    switch_collection_interval_minutes: int = 30
    sla_target: float = 99.9
    coverage_threshold: float = 90.0
    sla_timezone: str = "America/Vancouver"
    sla_backfill_start: str = "2026-01-01"
    availability_source: str = "Ping"
    sla_query_hours: int = 8

    @field_validator("lm_portal_url")
    @classmethod
    def validate_portal(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("LogicMonitor portal must be an HTTPS origin")
        return f"https://{parsed.hostname}"

    def secret(self, value: str, file_name: str) -> str:
        if file_name:
            return Path(file_name).read_text(encoding="utf-8").strip()
        return value

    @property
    def access_id(self): return self.secret(self.lm_access_id, self.lm_access_id_file)
    @property
    def access_key(self): return self.secret(self.lm_access_key, self.lm_access_key_file)
    @property
    def signing_secret(self): return self.secret(self.jwt_secret, self.jwt_secret_file)
    @property
    def admin_password(self): return self.secret(self.local_admin_password, self.local_admin_password_file)
    @property
    def user_password(self): return self.secret(self.local_user_password, self.local_user_password_file)


@lru_cache
def get_settings() -> Settings:
    return Settings()
