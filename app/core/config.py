"""Environment configuration for the BUMA Master PPBase project."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql+asyncpg://ppbase:ppbase@localhost:55434/buma_master_ppbase"
    host: str = "127.0.0.1"
    port: int = 8097
    data_dir: str = "pb_data"
    migrations_dir: str = "pb_migrations"
    enable_internal_jobs: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.getenv("PPBASE_DATABASE_URL", cls.database_url),
            host=os.getenv("PPBASE_HOST", cls.host),
            port=int(os.getenv("PPBASE_PORT", str(cls.port))),
            data_dir=os.getenv("PPBASE_DATA_DIR", cls.data_dir),
            migrations_dir=os.getenv("PPBASE_MIGRATIONS_DIR", cls.migrations_dir),
            enable_internal_jobs=_env_bool("BUMA_MASTER_ENABLE_INTERNAL_JOBS", False),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
