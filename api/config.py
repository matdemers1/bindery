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

    # Overridable in Settings; the closed set lives in api/models.py.
    bindery_model: str = "claude-opus-5"
    bindery_prompt_version: str = "v1"
    anthropic_api_key: str = ""
    # A push endpoint (ntfy, Pushover, Gotify, a Slack hook). Empty means
    # notifications are off, which is a supported configuration, not an error.
    notify_webhook_url: str = ""

    # Offsite replication (ADR-010). All five are overridable in Settings; the
    # environment is only the fallback, so a fresh host needs no compose edit.
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    offsite_bucket: str = ""
    offsite_region: str = "us-east-1"
    offsite_kms_key_id: str = ""

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


# --------------------------------------------------------------------------
# Startup validation
# --------------------------------------------------------------------------

# The placeholder that ships in .env.example and in the ZimaOS manifest. It is
# in the repository, so a deployment still carrying it is not weakly configured
# — it is unconfigured, and the difference is invisible from the outside.
PLACEHOLDER_JWT_SECRET = "change-me-to-a-long-random-string"
PLACEHOLDER_MARKER = "change-me"
# Not an entropy check — a real secret is 32+ random characters and the message
# below says so. This floor only catches someone typing a word into the
# variable, and is set low enough that the short values CI and local
# development deliberately use still boot.
MIN_JWT_SECRET_LENGTH = 16


class UnusableConfiguration(RuntimeError):
    """The process must not start with this configuration."""


def configuration_problems(settings: Settings) -> list[str]:
    """Every reason this configuration must not be served, each naming its variable.

    `jwt_secret` is the one that cannot be allowed to fail quietly. It signs
    every session (ADR-008) and `api/settings_store.py` derives the key
    protecting the stored AWS and Anthropic credentials from it, so a
    placeholder means anyone holding the repository can mint an administrator
    session and read the offsite credentials out of a dump. A wrong
    DATABASE_URL announces itself on the first query; a wrong JWT_SECRET never
    announces itself at all.
    """
    problems: list[str] = []

    secret = settings.jwt_secret.strip()
    if not secret or secret == PLACEHOLDER_JWT_SECRET or PLACEHOLDER_MARKER in secret:
        problems.append(
            "JWT_SECRET is still the placeholder from .env.example. It signs every "
            "session and protects the stored offsite credentials, and its value is "
            "public. Set JWT_SECRET to at least 32 random characters — "
            "`openssl rand -base64 48` — and restart."
        )
    elif len(secret) < MIN_JWT_SECRET_LENGTH:
        problems.append(
            f"JWT_SECRET is {len(secret)} characters, which is short enough to be "
            "guessed. Set it to at least 32 random characters — "
            "`openssl rand -base64 48` — and restart."
        )

    if PLACEHOLDER_MARKER in settings.database_url:
        problems.append(
            "DATABASE_URL still contains the .env.example placeholder "
            f"{PLACEHOLDER_MARKER!r}. Set it to the real connection string."
        )

    return problems


def require_usable_configuration(settings: Settings | None = None) -> None:
    """Refuse to start rather than to serve. See `configuration_problems`."""
    problems = configuration_problems(settings or get_settings())
    if problems:
        raise UnusableConfiguration(
            "Bindery refuses to start: " + " ".join(problems)
        )
