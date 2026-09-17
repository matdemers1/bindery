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

# Offsite replication (T-13.2, REQ-159, ADR-010). Only the secret access key is
# actually a secret: an AWS access key id is an *identifier*, not a credential —
# it appears in CloudTrail, in error messages, and in AWS's own console — and
# rendering it in full is what makes "which credential is loaded?" answerable
# during a rotation. Masking it would hide nothing and cost the one question the
# field exists to answer.
AWS_ACCESS_KEY_ID = "aws_access_key_id"
AWS_SECRET_ACCESS_KEY = "aws_secret_access_key"
OFFSITE_BUCKET = "offsite_bucket"
OFFSITE_REGION = "offsite_region"
# Deliberately not a secret, and deliberately stored in the clear. A restore has
# to know which key to ask for, and if that answer were only inside the archive
# it would be unreachable at exactly the moment it is needed.
OFFSITE_KMS_KEY_ID = "offsite_kms_key_id"

# Sign in with D3 Auth (Phase 20, REQ-203). Configured rather than compiled in:
# an operator turns SSO on without redeploying, and an archive whose operator
# never does is never told about a provider they have not heard of.
#
# `SSO_MODE` is `off`, `optional` or `required`. The client secret is a secret;
# the issuer and the client id are not — both are printed on the provider's own
# connection sheet and appear in every authorization URL.
OIDC_ISSUER = "oidc_issuer"
OIDC_CLIENT_ID = "oidc_client_id"
OIDC_CLIENT_SECRET = "oidc_client_secret"
SSO_MODE = "sso_mode"

SECRET_KEYS = frozenset(
    {ANTHROPIC_API_KEY, NOTIFY_WEBHOOK_URL, AWS_SECRET_ACCESS_KEY, OIDC_CLIENT_SECRET}
)
WRITABLE = frozenset(
    {
        ANTHROPIC_API_KEY,
        BINDERY_MODEL,
        PROMPT_VERSION,
        NOTIFY_WEBHOOK_URL,
        AWS_ACCESS_KEY_ID,
        AWS_SECRET_ACCESS_KEY,
        OFFSITE_BUCKET,
        OFFSITE_REGION,
        OFFSITE_KMS_KEY_ID,
        OIDC_ISSUER,
        OIDC_CLIENT_ID,
        OIDC_CLIENT_SECRET,
        SSO_MODE,
    }
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
        AWS_ACCESS_KEY_ID: environment.aws_access_key_id,
        AWS_SECRET_ACCESS_KEY: environment.aws_secret_access_key,
        OFFSITE_BUCKET: environment.offsite_bucket,
        OFFSITE_REGION: environment.offsite_region,
        OFFSITE_KMS_KEY_ID: environment.offsite_kms_key_id,
        OIDC_ISSUER: environment.oidc_issuer,
        OIDC_CLIENT_ID: environment.oidc_client_id,
        OIDC_CLIENT_SECRET: environment.oidc_client_secret,
        SSO_MODE: environment.sso_mode,
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
