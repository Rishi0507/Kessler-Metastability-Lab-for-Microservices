from __future__ import annotations

import os
import socket
from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KESSLER_", env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = "dev"
    instance: str = Field(default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}")

    host: str = "127.0.0.1"
    port: int = 9001
    public_url: str = "http://localhost:8080"
    trusted_proxies: str = "127.0.0.1"

    database_url: str = "postgresql://kessler:kessler@127.0.0.1:5433/kessler"
    database_replica_url: str | None = None
    db_pool_min: int = 1
    db_pool_max: int = 10
    db_statement_cache_size: int = 256
    db_command_timeout_s: float = 10.0
    redis_url: str = "redis://127.0.0.1:6380/0"

    jwt_secret: SecretStr = SecretStr("dev-only-jwt-secret-change-me")
    stream_secret: SecretStr = SecretStr("dev-only-stream-secret-change-me")
    access_ttl_s: int = 900
    refresh_ttl_s: int = 30 * 86_400
    cookie_secure: bool = False

    max_active_analyses_per_user: int = 3
    max_live_runs_per_user: int = 2
    max_sims_per_analysis: int = 600
    max_events_per_sim: int = 80_000_000
    admission_queue_limit: int = 20_000

    worker_threads: int = Field(default_factory=lambda: os.cpu_count() or 2)
    worker_live_slots: int = 8
    worker_nice: int = Field(default=10, ge=0, le=19)
    live_tick_s: float = 0.25
    task_claim_idle_s: float = 30.0
    task_max_deliveries: int = 5
    dispatch_prefetch: float = 1.5

    log_level: str = "INFO"
    otlp_endpoint: str | None = None

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.env == "prod":
            for name in ("jwt_secret", "stream_secret"):
                if getattr(self, name).get_secret_value().startswith("dev-only"):
                    raise ValueError(f"KESSLER_{name.upper()} must be set in production")
            if not self.cookie_secure:
                raise ValueError("KESSLER_COOKIE_SECURE must be true in production")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
