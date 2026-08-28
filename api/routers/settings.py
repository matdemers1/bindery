"""Settings (screen 19).

The API key above all: "add your key" is the first thing anyone does, and
shelling into a NAS to edit a compose file is a bad answer to it.

**A secret is never returned.** The client learns whether one is configured and
sees the last four characters, and that is all — a settings form that renders
your API key into the DOM has leaked it to every browser extension you run.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import events, models, settings_store
from api.audit import record
from api.auth.dependencies import current_user
from api.db import repository
from api.db.enums import ActorType
from api.db.models import AppUser
from api.db.session import get_session
from api.schemas import (
    ModelChoiceOut,
    SettingsOut,
    SettingsTestOut,
    SettingsUpdateIn,
)

router = APIRouter(prefix="/settings", tags=["settings"])


async def _owner_only(session: AsyncSession, user: AppUser) -> None:
    """Settings are account-wide, so only someone who owns a library may change them."""
    if not await repository.writable_library_ids(session, user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "owner access required")


@router.get("", response_model=SettingsOut)
async def read_settings(
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SettingsOut:
    key = await settings_store.get(session, settings_store.ANTHROPIC_API_KEY)
    webhook = await settings_store.get(session, settings_store.NOTIFY_WEBHOOK_URL)
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
    )


@router.put("", response_model=SettingsOut)
async def update_settings(
    payload: SettingsUpdateIn,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SettingsOut:
    await _owner_only(session, user)

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
