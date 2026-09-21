"""Settings (screen 19).

The API key above all: "add your key" is the first thing anyone does, and
shelling into a NAS to edit a compose file is a bad answer to it.

**A secret is never returned.** The client learns whether one is configured and
sees the last four characters, and that is all — a settings form that renders
your API key into the DOM has leaked it to every browser extension you run.
"""

import ipaddress
import re
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import ai_client, events, models, offsite, settings_store
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


def admin_only(user: AppUser) -> None:
    """The offsite destination is a statement about the host, not about a library.

    `_owner_only` is satisfied by owning *any* library, and every invited
    account owns its own personal library (`api/accounts.py`) — so it let any
    household member repoint replication at a bucket they control, and the next
    run would have shipped the pg_dump, every blob and every vault object there
    (ADR-010). ADR-009 puts storage and the pipeline with the administrator, and
    that is exactly what this is.

    Public, and imported by `api/routers/trust.py` for the replicate trigger,
    so the configuration and the thing it configures are gated by one
    definition rather than two that can drift apart.

    403 rather than the admin panel's 404: these routes exist for everyone — the
    model and the API key are still owner-writable, and the replication *status*
    is readable by any member — so there is nothing to hide by pretending they
    are missing, and the person needs to know who to ask.
    """
    if not user.is_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Offsite replication is the archive administrator's to configure and run.",
        )


# Written by one rule rather than five near-identical blocks: None leaves alone,
# empty clears. Module level because both the write loop and the admin gate that
# guards it have to agree on exactly which fields these are.
_OFFSITE_FIELDS: tuple[tuple[str, str], ...] = (
    ("aws_access_key_id", settings_store.AWS_ACCESS_KEY_ID),
    ("aws_secret_access_key", settings_store.AWS_SECRET_ACCESS_KEY),
    ("offsite_bucket", settings_store.OFFSITE_BUCKET),
    ("offsite_region", settings_store.OFFSITE_REGION),
    ("offsite_kms_key_id", settings_store.OFFSITE_KMS_KEY_ID),
)
# The provider half, by the same rule. `sso_mode` is validated rather than written blind, so it
# is not in this table.
_OIDC_FIELDS: tuple[tuple[str, str], ...] = (
    ("oidc_issuer", settings_store.OIDC_ISSUER),
    ("oidc_client_id", settings_store.OIDC_CLIENT_ID),
    ("oidc_client_secret", settings_store.OIDC_CLIENT_SECRET),
)

SSO_MODES = ("off", "optional", "required")


def sso_admin_only(user: AppUser) -> None:
    """Which provider may mint sessions here is the administrator's, for the same reason.

    An account that could point this at a provider it controls could grant itself `admin` there
    and arrive here holding it — the whole archive, through a settings form. `_owner_only` is
    satisfied by owning any library, and every invited account owns one.
    """
    if not user.is_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Sign in with D3 Auth is the archive administrator's to configure.",
        )


# The secret is excluded on purpose: the audit records *that* it changed, never
# a value. The other four are identifiers, and "which bucket was it before" is
# the question asked after a destination has been swapped.
_OFFSITE_AUDITED = tuple(
    (field, key) for field, key in _OFFSITE_FIELDS if field != "aws_secret_access_key"
)


# S3 bucket naming, the subset that matters: 3-63 characters, lowercase
# alphanumeric plus hyphens and dots, starting and ending alphanumeric.
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
# Long-term user keys are AKIA…; temporary session credentials are ASIA… and
# would expire mid-backup, so they are refused rather than accepted.
_ACCESS_KEY_ID = re.compile(r"^AKIA[A-Z0-9]{12,124}$")


# Schemes `urllib.request` will happily open that are not a webhook. `file:`
# turns the notifier into a file reader, `ftp:` into an outbound transfer.
_WEBHOOK_SCHEMES = frozenset({"http", "https"})


def _reject(detail: str) -> None:
    """Refuse here rather than at 3am on the first replication run.

    The precedent is the model picker above: a bad value accepted now becomes an
    unexplained failure hours later, in a component that did not cause it.
    """
    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail)


def _check_webhook(url: str) -> None:
    """Refuse a notification URL that is not a webhook (server-side request forgery).

    `api/notify.py` hands this value to `urllib.request.urlopen` from inside the
    api and worker containers. The only validation on the way in was `.strip()`,
    which made the field a general-purpose "make the server fetch this" control
    for anyone who could save settings: `file:///data/...` reads a file,
    `http://169.254.169.254/...` asks the cloud metadata service, and any
    container on the compose network is one hostname away.

    Validated here, where it is *stored*, rather than in the notifier: the
    notifier runs unattended at 3am inside an exception handler that must never
    raise, which is the worst possible place to discover a bad value, and the
    person who typed it is standing right here.

    What is deliberately still allowed is a private address. A self-hosted
    archive notifying a self-hosted ntfy on the same LAN is the *named* use case
    in `api/notify.py` — "a self-hosted archive should not require an account
    with anybody" — so refusing RFC1918 would remove the feature rather than
    secure it. Loopback and link-local are refused, because neither is ever a
    webhook: loopback inside the container is the api talking to itself, and
    link-local is the metadata endpoint and nothing else.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _WEBHOOK_SCHEMES:
        _reject(
            f"{url!r} is not an http(s) URL. A notification webhook is a URL the "
            "server will POST to, so only http and https are accepted."
        )
    host = (parsed.hostname or "").strip()
    if not host:
        _reject(f"{url!r} names no host.")
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        _reject(
            "localhost inside the container is the archive talking to itself, "
            "not a notification service. Use the address or name the notifier "
            "is actually reachable at."
        )

    # A literal address is checked directly; a name is not resolved here on
    # purpose. Resolving at save time proves nothing about what the name will
    # answer at 3am, and a DNS lookup inside a request handler is its own
    # availability problem. Blocking the literal forms closes the shape that is
    # actually used, and `api/notify.py` discards the response body, so what
    # remains is blind.
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return
    if address.is_loopback or address.is_link_local or address.is_unspecified:
        _reject(
            f"{host} is not somewhere the archive will send notifications — "
            "loopback and link-local addresses are the server talking to itself "
            "and the host's metadata service, never a webhook."
        )
    if address.is_multicast or address.is_reserved:
        _reject(f"{host} is not a routable address for a webhook.")


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


async def _check_sso(session: AsyncSession, payload: SettingsUpdateIn) -> None:
    """Refuse a configuration the sign-in screen could not act on.

    The failure this exists to prevent is a *visible* one: turning SSO on with no issuer puts a
    button on the front door that cannot work, and in `required` mode it is the front door.
    """
    if payload.oidc_issuer:
        issuer = payload.oidc_issuer.strip()
        parsed = urlparse(issuer)
        if parsed.scheme != "https" or not parsed.netloc:
            _reject(
                f"{issuer!r} is not an https issuer URL. Copy it from the provider's "
                "connection sheet — ID tokens are verified against it character for character."
            )
        if parsed.query or parsed.fragment:
            _reject("An issuer is an origin and an optional path, with no query or fragment.")

    mode = (payload.sso_mode or "").strip().lower()
    if payload.sso_mode is not None and mode not in SSO_MODES:
        _reject(f"{mode!r} is not off, optional or required.")

    if mode in ("optional", "required"):
        # What the archive will hold *after* this write, not what it holds now: the form sends
        # the provider and the mode together, and checking the stored values would refuse the
        # one request that configures everything at once.
        async def settled(field: str, key: str) -> str:
            sent = getattr(payload, field)
            if sent is not None:
                return sent.strip()
            return (await settings_store.get(session, key) or "").strip()

        missing = [
            name
            for name, field, key in (
                ("an issuer", "oidc_issuer", settings_store.OIDC_ISSUER),
                ("a client id", "oidc_client_id", settings_store.OIDC_CLIENT_ID),
                ("a client secret", "oidc_client_secret", settings_store.OIDC_CLIENT_SECRET),
            )
            if not await settled(field, key)
        ]
        if missing:
            _reject(
                f"Turning SSO {mode} needs {', '.join(missing)}. Register Bindery at the "
                "provider first and copy its connection sheet."
            )


@router.get("", response_model=SettingsOut)
async def read_settings(
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SettingsOut:
    """What is configured. The offsite half is the administrator's alone.

    The write path has been the administrator's since SEC-02 (`admin_only`); the
    read path asked for nothing beyond being signed in, and returned the exact
    bucket, region, KMS key and IAM access key id holding the offsite copy of
    the entire archive, plus the last four characters of the secret. Every
    household member — a READER who owns nothing included — was handed the
    reconnaissance half for free: where the backup of everyone's documents
    lives, and a four-character check on the credential for it.

    ADR-009's principle is that an account learns about its own material and
    nothing else, and the offsite destination is nobody's material but the
    host's. So the response splits: the model, the API key state and the webhook
    state are still everyone's, because the panel that renders them is; the
    offsite identity is redacted to the same `None` a non-configured archive
    returns, which says nothing at all rather than saying "not for you".
    """
    key = await settings_store.get(session, settings_store.ANTHROPIC_API_KEY)
    webhook = await settings_store.get(session, settings_store.NOTIFY_WEBHOOK_URL)
    aws_secret = await settings_store.get(session, settings_store.AWS_SECRET_ACCESS_KEY)
    offsite_visible = user.is_admin
    oidc_secret = await settings_store.get(session, settings_store.OIDC_CLIENT_SECRET)
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
        aws_access_key_id=(
            await settings_store.get(session, settings_store.AWS_ACCESS_KEY_ID)
            if offsite_visible
            else None
        ),
        aws_secret_configured=bool(aws_secret) if offsite_visible else False,
        aws_secret_hint=settings_store.mask(aws_secret) if offsite_visible else None,
        offsite_bucket=(
            await settings_store.get(session, settings_store.OFFSITE_BUCKET)
            if offsite_visible
            else None
        ),
        offsite_region=(
            await settings_store.get(session, settings_store.OFFSITE_REGION)
            if offsite_visible
            else None
        ),
        offsite_kms_key_id=(
            await settings_store.get(session, settings_store.OFFSITE_KMS_KEY_ID)
            if offsite_visible
            else None
        ),
        oidc_issuer=(
            await settings_store.get(session, settings_store.OIDC_ISSUER)
            if offsite_visible
            else None
        ),
        oidc_client_id=(
            await settings_store.get(session, settings_store.OIDC_CLIENT_ID)
            if offsite_visible
            else None
        ),
        oidc_secret_configured=bool(oidc_secret) if offsite_visible else False,
        oidc_secret_hint=settings_store.mask(oidc_secret) if offsite_visible else None,
        # The mode is not redacted: `/auth/oidc/status` already tells an anonymous browser
        # whether this archive offers SSO, because the sign-in screen has to render something.
        sso_mode=await settings_store.get(session, settings_store.SSO_MODE) or "off",
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
    await _check_sso(session, payload)

    before: dict[str, str | None] = {}
    if any(getattr(payload, field) is not None for field, _ in _OFFSITE_FIELDS):
        admin_only(user)
        # Read before the first write, so the audit answers "where was it
        # pointing before?" — the question a swapped destination raises.
        for field, setting_key in _OFFSITE_AUDITED:
            before[field] = await settings_store.get(session, setting_key)

    sso_sent = [field for field, _ in _OIDC_FIELDS if getattr(payload, field) is not None]
    if sso_sent or payload.sso_mode is not None:
        sso_admin_only(user)
        for field, setting_key in _OIDC_FIELDS:
            if field != "oidc_client_secret":
                before[field] = await settings_store.get(session, setting_key)
        before["sso_mode"] = await settings_store.get(session, settings_store.SSO_MODE)

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
        webhook = payload.notify_webhook_url.strip()
        if webhook:
            # An empty string still clears it; anything else has to be a webhook.
            _check_webhook(webhook)
        await settings_store.set_(
            session, settings_store.NOTIFY_WEBHOOK_URL, webhook or None, actor_id=user.id,
        )
        changed.append("notify_webhook_url")

    # The offsite fields take no interpretation, so they are written by the same
    # rule (see `_OFFSITE_FIELDS`). Validation and the admin gate already
    # happened above.
    for field, setting_key in _OFFSITE_FIELDS:
        value = getattr(payload, field)
        if value is not None:
            await settings_store.set_(
                session, setting_key, value.strip() or None, actor_id=user.id
            )
            changed.append(field)

    for field, setting_key in _OIDC_FIELDS:
        value = getattr(payload, field)
        if value is not None:
            await settings_store.set_(
                session, setting_key, value.strip() or None, actor_id=user.id
            )
            changed.append(field)
    if payload.sso_mode is not None:
        await settings_store.set_(
            session, settings_store.SSO_MODE,
            payload.sso_mode.strip().lower(), actor_id=user.id,
        )
        changed.append("sso_mode")

    if changed:
        # The audit records *that* a secret changed, never its value.
        await record(
            session,
            entity_type="setting",
            entity_id=user.id,
            action="update_settings",
            actor_type=ActorType.HUMAN,
            actor_id=user.id,
            before=before or None,
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
    # Through the same construction point as the pipeline and Ask. It used to
    # build its own client inline, which meant "check my key" exercised a code
    # path nothing else runs — a test that passes against a client the pipeline
    # does not use is a test of the wrong thing (BND-FR-002).
    config = await ai_client.resolve(session)
    client = ai_client.build_client(config.api_key)
    if client is None:
        return SettingsTestOut(
            ok=False,
            detail="No API key is configured. The archive works without one — "
                   "documents are still OCR'd, indexed and searchable, and only "
                   "classification defers.",
        )

    model = config.model
    try:
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

    Administrator-only, like every other action on this panel: it spends money,
    however little, and it writes to the bucket whose credentials only an
    administrator can set.
    """
    await _owner_only(session, user)
    admin_only(user)
    result = await offsite.probe(await offsite.config_from_settings(session))
    return OffsiteTestOut(
        ok=result.ok,
        detail=result.detail,
        encryption=result.encryption,
        kms_key_arn=result.kms_key_arn,
        bucket_key_enabled=result.bucket_key_enabled,
        checks=result.checks,
    )
