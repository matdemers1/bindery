"""Settings (screen 19).

The API key above all: "add your key" is the first thing anyone does, and
shelling into a NAS to edit a compose file is a bad answer to it.

**A secret is never returned.** The client learns whether one is configured and
sees the last four characters, and that is all — a settings form that renders
your API key into the DOM has leaked it to every browser extension you run.
"""

import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import events, models, offsite, settings_store
from api.audit import record
from api.auth.dependencies import current_user
from api.db import repository
from api.db.enums import ActorType
from api.db.models import AppUser
from api.db.session import get_session
from api.schemas import (
    ModelChoiceOut,
    OffsiteTestOut,
    SettingsOut,
    SettingsTestOut,
    SettingsUpdateIn,
)

router = APIRouter(prefix="/settings", tags=["settings"])


async def _owner_only(session: AsyncSession, user: AppUser) -> None:
    """Settings are account-wide, so only someone who owns a library may change them."""
    if not await repository.writable_library_ids(session, user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "owner access required")


# S3 bucket naming, the subset that matters: 3-63 characters, lowercase
# alphanumeric plus hyphens and dots, starting and ending alphanumeric.
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
# Long-term user keys are AKIA…; temporary session credentials are ASIA… and
# would expire mid-backup, so they are refused rather than accepted.
_ACCESS_KEY_ID = re.compile(r"^AKIA[A-Z0-9]{12,124}$")


def _reject(detail: str) -> None:
    """Refuse here rather than at 3am on the first replication run.

    The precedent is the model picker above: a bad value accepted now becomes an
    unexplained failure hours later, in a component that did not cause it.
    """
    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail)


def _check_offsite(payload: SettingsUpdateIn) -> None:
    if payload.offsite_bucket:
        name = payload.offsite_bucket.strip()
        if not _BUCKET.match(name):
            _reject(
                f"{name!r} is not a valid S3 bucket name — 3-63 characters, "
                "lowercase letters, digits, hyphens and dots only."
            )
    if payload.offsite_region:
        region = payload.offsite_region.strip()
        if not _REGION.match(region):
            _reject(f"{region!r} does not look like an AWS region (e.g. us-east-1).")

    key_id = (payload.aws_access_key_id or "").strip()
    if key_id:
        if key_id.startswith("ASIA"):
            _reject(
                "That is a temporary session credential (ASIA…). It expires, and "
                "it would expire mid-backup. Create a long-term access key for the "
                "bindery-offsite IAM user instead."
            )
        if not _ACCESS_KEY_ID.match(key_id):
            # The mistake this actually catches: the two fields filled in the
            # wrong order. A 40-character secret in the id field is otherwise
            # stored happily and fails much later as an opaque 403.
            _reject(
                "That does not look like an AWS access key id (they begin AKIA). "
                "Check the two fields are not swapped — the secret goes below."
            )


@router.get("", response_model=SettingsOut)
async def read_settings(
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SettingsOut:
    key = await settings_store.get(session, settings_store.ANTHROPIC_API_KEY)
    webhook = await settings_store.get(session, settings_store.NOTIFY_WEBHOOK_URL)
    aws_secret = await settings_store.get(session, settings_store.AWS_SECRET_ACCESS_KEY)
    return SettingsOut(
        available_models=[
            ModelChoiceOut(
                id=choice.id, name=choice.name, blurb=choice.blurb,
                input_per_mtok=choice.input, output_per_mtok=choice.output,
            )
            for choice in models.MODELS
        ],
        anthropic_key_configured=bool(key),
        anthropic_key_hint=settings_store.mask(key),
        model=await settings_store.get(session, settings_store.BINDERY_MODEL) or "claude-opus-5",
        prompt_version=await settings_store.get(session, settings_store.PROMPT_VERSION) or "v1",
        notify_webhook_configured=bool(webhook),
        notify_webhook_hint=settings_store.mask(webhook),
        aws_access_key_id=await settings_store.get(
            session, settings_store.AWS_ACCESS_KEY_ID
        ),
        aws_secret_configured=bool(aws_secret),
        aws_secret_hint=settings_store.mask(aws_secret),
        offsite_bucket=await settings_store.get(session, settings_store.OFFSITE_BUCKET),
        offsite_region=await settings_store.get(session, settings_store.OFFSITE_REGION),
        offsite_kms_key_id=await settings_store.get(
            session, settings_store.OFFSITE_KMS_KEY_ID
        ),
    )


@router.put("", response_model=SettingsOut)
async def update_settings(
    payload: SettingsUpdateIn,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SettingsOut:
    await _owner_only(session, user)
    # Validated up front so the refusal is cheap and names the field. Note that
    # this ordering is *not* what makes the update atomic — `get_session` never
    # commits on an exception, so the transaction is what actually prevents a
    # half-applied configuration. Moving this call below the writes changes
    # nothing observable, which is worth knowing before someone "tidies" it and
    # believes they have broken something.
    _check_offsite(payload)

    changed: list[str] = []
    if payload.anthropic_api_key is not None:
        # An empty string clears it — that is how you turn classification off
        # without losing anything else.
        await settings_store.set_(
            session, settings_store.ANTHROPIC_API_KEY,
            payload.anthropic_api_key.strip() or None, actor_id=user.id,
        )
        changed.append("anthropic_api_key")
    if payload.model is not None:
        chosen = payload.model.strip()
        if not models.is_valid(chosen):
            # Refused here rather than accepted and discovered by the worker.
            # An unusable model id fails every classification, hours later, as a
            # queue full of dead letters with no obvious cause.
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"{chosen!r} is not one of the available models",
            )
        await settings_store.set_(
            session, settings_store.BINDERY_MODEL, chosen, actor_id=user.id
        )
        changed.append("model")
    if payload.prompt_version is not None:
        await settings_store.set_(
            session, settings_store.PROMPT_VERSION, payload.prompt_version.strip(),
            actor_id=user.id,
        )
        changed.append("prompt_version")

    if payload.notify_webhook_url is not None:
        await settings_store.set_(
            session, settings_store.NOTIFY_WEBHOOK_URL,
            payload.notify_webhook_url.strip() or None, actor_id=user.id,
        )
        changed.append("notify_webhook_url")

    # The offsite fields take no interpretation, so they are written by the same
    # rule rather than five near-identical blocks: None leaves alone, empty
    # clears. Validation already happened above.
    for field, setting_key in (
        ("aws_access_key_id", settings_store.AWS_ACCESS_KEY_ID),
        ("aws_secret_access_key", settings_store.AWS_SECRET_ACCESS_KEY),
        ("offsite_bucket", settings_store.OFFSITE_BUCKET),
        ("offsite_region", settings_store.OFFSITE_REGION),
        ("offsite_kms_key_id", settings_store.OFFSITE_KMS_KEY_ID),
    ):
        value = getattr(payload, field)
        if value is not None:
            await settings_store.set_(
                session, setting_key, value.strip() or None, actor_id=user.id
            )
            changed.append(field)

    if changed:
        # The audit records *that* a secret changed, never its value.
        await record(
            session,
            entity_type="setting",
            entity_id=user.id,
            action="update_settings",
            actor_type=ActorType.HUMAN,
            actor_id=user.id,
            after={"changed": changed},
        )
        await events.publish(session, events.Topic.SETTINGS)
    await session.commit()
    return await read_settings(user=user, session=session)


@router.post("/test-ai", response_model=SettingsTestOut)
async def test_ai(
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SettingsTestOut:
    """Check the key against the real API, now, rather than at the first document.

    An invalid key that only surfaces when a scan arrives looks like a broken
    pipeline. This is the one place Bindery deliberately makes a live API call
    on a human's behalf, and it is a cheap one.
    """
    key = await settings_store.get(session, settings_store.ANTHROPIC_API_KEY)
    if not key:
        return SettingsTestOut(
            ok=False,
            detail="No API key is configured. The archive works without one — "
                   "documents are still OCR'd, indexed and searchable, and only "
                   "classification defers.",
        )

    model = await settings_store.get(session, settings_store.BINDERY_MODEL) or "claude-opus-5"
    try:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=key)
        response = await client.messages.create(
            model=model,
            max_tokens=16,
            messages=[{"role": "user", "content": "Reply with the single word: ready"}],
        )
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return SettingsTestOut(
            ok=True,
            detail=f"{model} responded: {text[:40] or '(empty)'}",
            model=model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
    except Exception as exc:  # surfaced verbatim — the message is the diagnosis
        name = type(exc).__name__
        return SettingsTestOut(ok=False, detail=f"{name}: {str(exc)[:300]}", model=model)


@router.post("/test-offsite", response_model=OffsiteTestOut)
async def test_offsite(
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> OffsiteTestOut:
    """Prove a backup would actually survive, before one is ever taken.

    Deliberately a write and a read rather than a `ListBucket`. Reachability
    proves the credential exists; it proves nothing about whether S3 will accept
    an object encrypted with the configured key, or hand it back. Those are four
    separate permissions — PutObject, kms:GenerateDataKey, GetObject,
    kms:Decrypt — and they fail independently.

    Owner-only, like every other write on this screen: it costs money, however
    little, and it writes to the bucket.
    """
    await _owner_only(session, user)
    result = await offsite.probe(await offsite.config_from_settings(session))
    return OffsiteTestOut(
        ok=result.ok,
        detail=result.detail,
        encryption=result.encryption,
        kms_key_arn=result.kms_key_arn,
        bucket_key_enabled=result.bucket_key_enabled,
        checks=result.checks,
    )
