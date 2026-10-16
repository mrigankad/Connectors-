"""Runtime settings. Secrets come from the environment or a secret backend, never from code."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WAYAM_", env_file=".env", extra="ignore")

    env: str = "development"
    database_url: str = "postgresql://wayam:wayam@127.0.0.1:5432/wayam_connectors"
    test_database_url: str | None = None
    secret_backend: str = Field(default="local", description="local | azure_key_vault")
    local_secret_key: str | None = Field(default=None, description="base64 32 bytes for the local backend")
    azure_key_vault_url: str | None = None
    job_lease_seconds: int = 300
    max_job_attempts: int = 5
    otel_enabled: bool = False

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.is_production and s.secret_backend == "local":
        raise RuntimeError("WAYAM_SECRET_BACKEND=local is not allowed in production")
    return s
