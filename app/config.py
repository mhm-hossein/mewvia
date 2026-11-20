"""Application settings, loaded from environment / .env once at startup."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Public Bot API hard limits. The local Bot API server raises both to ~2GB;
# see the commented service in docker-compose.yml.
PUBLIC_API_DOWNLOAD_LIMIT = 20 * 1024 * 1024
PUBLIC_API_UPLOAD_LIMIT = 50 * 1024 * 1024
LOCAL_API_FILE_LIMIT = 2 * 1000 * 1000 * 1000


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Telegram ----
    bot_token: str
    bot_username: str = "MewviaBot"
    # NoDecode: keep pydantic-settings from JSON-parsing the env value so the
    # validator below can accept the friendlier "1,2,3" form.
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # Empty => public api.telegram.org. Set to the local server to lift file limits.
    telegram_api_url: str = ""
    telegram_api_id: str = ""
    telegram_api_hash: str = ""

    # ---- Database ----
    postgres_db: str = "mewvia"
    postgres_user: str = "mewvia"
    postgres_password: str = ""
    postgres_host: str = "postgres"
    postgres_port: int = 5432

    # ---- Redis ----
    redis_url: str = "redis://redis:6379/0"

    # ---- Runtime ----
    log_level: str = "INFO"
    log_json: bool = True
    tmp_dir: Path = Path("/data/tmp")
    cookie_dir: Path = Path("/data/cookies")
    backup_dir: Path = Path("/data/backups")
    health_file: Path = Path("/data/health")

    max_concurrent_jobs: int = 6
    job_timeout: int = 120
    throttle_seconds: int = 2

    # ---- Premium emoji ----
    premium_emoji_enabled: bool = True

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_admin_ids(cls, v: object) -> object:
        """Accept "1,2,3" as well as a real list."""
        if isinstance(v, str):
            return [int(part) for part in v.replace(" ", "").split(",") if part]
        return v

    @field_validator("bot_username", mode="before")
    @classmethod
    def _strip_at(cls, v: object) -> object:
        if isinstance(v, str):
            return v.lstrip("@")
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v

    # ---- Derived ----
    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def alembic_database_url(self) -> str:
        """Alembic runs with the same async driver; kept separate for clarity."""
        return self.database_url

    @property
    def uses_local_api(self) -> bool:
        return bool(self.telegram_api_url)

    @property
    def download_limit(self) -> int:
        return LOCAL_API_FILE_LIMIT if self.uses_local_api else PUBLIC_API_DOWNLOAD_LIMIT

    @property
    def upload_limit(self) -> int:
        return LOCAL_API_FILE_LIMIT if self.uses_local_api else PUBLIC_API_UPLOAD_LIMIT

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.admin_ids


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor so every module sees the same instance."""
    # All required values (bot_token, ...) come from the environment / .env.
    return Settings()
