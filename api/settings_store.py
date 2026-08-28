"""Runtime settings, with secrets encrypted at rest.

Precedence is **database over environment**. A value set in the UI must win over
one baked into the compose file, or the UI looks broken.

Secrets are encrypted with a key derived from `JWT_SECRET`. That does not defend
against someone who already has the host — they have the environment too. It
defends against the case that actually happens: a database dump landing on a
backup drive, in an email, in a support ticket.
"""

import base64
import hashlib
import logging
import uuid

import sqlalchemy as sa
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.db.models import Setting

log = logging.getLogger("bindery.settings")

# Keys the UI may write. A closed set, so a stray request cannot invent config.
ANTHROPIC_API_KEY = "anthropic_api_key"
BINDERY_MODEL = "bindery_model"
PROMPT_VERSION = "bindery_prompt_version"
# Where a stalled pipeline goes to be noticed (REQ-110). Treated as a secret:
# most push services put the credential in the URL itself.
NOTIFY_WEBHOOK_URL = "notify_webhook_url"

SECRET_KEYS = frozenset({ANTHROPIC_API_KEY, NOTIFY_WEBHOOK_URL})
WRITABLE = frozenset(
    {ANTHROPIC_API_KEY, BINDERY_MODEL, PROMPT_VERSION, NOTIFY_WEBHOOK_URL}
)


def _cipher() -> Fernet:
    # Fernet needs 32 url-safe base64 bytes; JWT_SECRET is arbitrary text.
    digest = hashlib.sha256(get_settings().jwt_secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


async def get(session: AsyncSession, key: str) -> str | None:
    """Read a setting, decrypting if needed. Falls back to the environment."""
    row = (
        await session.execute(sa.select(Setting).where(Setting.key == key))
    ).scalar_one_or_none()

    if row is not None and row.value:
        if not row.is_secret:
            return row.value
        try:
            return _cipher().decrypt(row.value.encode()).decode()
        except InvalidToken:
            # JWT_SECRET changed, so the stored secret can no longer be read.
            # Say so rather than silently behaving as if nothing is configured.
            log.error(
                "cannot decrypt setting %r — JWT_SECRET has changed since it was "
                "saved. Re-enter it in Settings.", key,
            )
            return None

    environment = get_settings()
    return {
        ANTHROPIC_API_KEY: environment.anthropic_api_key,
        BINDERY_MODEL: environment.bindery_model,
        PROMPT_VERSION: environment.bindery_prompt_version,
        NOTIFY_WEBHOOK_URL: environment.notify_webhook_url,
    }.get(key) or None


async def set_(
    session: AsyncSession, key: str, value: str | None, *, actor_id: uuid.UUID | None
) -> None:
    if key not in WRITABLE:
        raise ValueError(f"{key!r} is not a writable setting")

    is_secret = key in SECRET_KEYS
    stored = None
    if value:
        stored = _cipher().encrypt(value.encode()).decode() if is_secret else value

    await session.execute(
        insert(Setting)
        .values(key=key, value=stored, is_secret=is_secret, updated_by=actor_id)
        .on_conflict_do_update(
            index_elements=[Setting.key],
            set_={"value": stored, "is_secret": is_secret,
                  "updated_by": actor_id, "updated_at": sa.func.now()},
        )
    )


def mask(value: str | None) -> str | None:
    """A hint that the right key is configured, without revealing it."""
    if not value:
        return None
    return f"…{value[-4:]}" if len(value) > 8 else "…"
