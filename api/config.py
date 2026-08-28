"""Runtime configuration, read from the environment (see .env.example)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://bindery:change-me@postgres:5432/bindery"

    jwt_secret: str = "change-me-to-a-long-random-string"
    jwt_access_ttl_minutes: int = 30
    jwt_refresh_ttl_days: int = 30
    jwt_algorithm: str = "HS256"
    cookie_secure: bool = True

    # Paths inside the container. HOST_DATA_ROOT is a compose concern only.
    data_root: Path = Path("/data")
    inbox_root: Path = Path("/data/inbox")

    worker_concurrency: int = 3
    ocr_languages: str = "eng"
    ocr_deskew: bool = True
    ocr_clean: bool = True

    bindery_model: str = "claude-opus-5"
    bindery_prompt_version: str = "v1"
    anthropic_api_key: str = ""
    # A push endpoint (ntfy, Pushover, Gotify, a Slack hook). Empty means
    # notifications are off, which is a supported configuration, not an error.
    notify_webhook_url: str = ""

    @property
    def blob_root(self) -> Path:
        """Content-addressed originals. Write-once; never modified (invariant 1)."""
        return self.data_root / "blobs"

    @property
    def temp_root(self) -> Path:
        """Uploads land here while streaming, before they have a content address."""
        return self.data_root / "tmp"

    @property
    def derived_root(self) -> Path:
        """Per-stage artifacts. Everything here is reproducible from a blob."""
        return self.data_root / "derived"


@lru_cache
def get_settings() -> Settings:
    return Settings()
