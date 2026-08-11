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


# Placeholder values that must never be used in production. `production_issues()` checks
# for these and refuses to start when environment=production, so a real deployment cannot
# accidentally run with development credentials.
_DEV_JWT = "development-only-change-me"
_DEV_ADMIN = "change-this-development-password"
_DEV_USER = "change-this-viewer-password"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    environment: str = "development"  # set to "production" to enforce the hardening checks
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
    # Proactive client-side cap on LogicMonitor API calls. The portal reports its own budget
    # via x-rate-limit-limit / x-rate-limit-window (measured: 700 requests per 60s). We stay
    # deliberately below it so a large backfill cannot exhaust the tenant-wide budget that
    # other LogicMonitor consumers (and live monitoring) also depend on. 0 disables the cap.
    lm_rate_limit_per_minute: int = 500

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

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in ("production", "prod")

    def production_issues(self) -> list[str]:
        """Return blocking misconfigurations for a production deployment (empty = OK)."""
        issues = []
        if self.signing_secret in ("", _DEV_JWT) or len(self.signing_secret) < 32:
            issues.append("JWT signing secret is the development default or shorter than 32 chars.")
        if self.admin_password in ("", _DEV_ADMIN):
            issues.append("Administrator password is still the development default.")
        if self.user_password in ("", _DEV_USER):
            issues.append("Read-only viewer password is still the development default.")
        if self.admin_password == self.user_password:
            issues.append("Administrator and viewer passwords must differ.")
        if "localhost" in self.allowed_origins or "127.0.0.1" in self.allowed_origins:
            issues.append("CORS allowed_origins still points at localhost.")
        if self.database_url.startswith("sqlite"):
            issues.append("Database is SQLite; use PostgreSQL for production.")
        return issues


@lru_cache
def get_settings() -> Settings:
    return Settings()
