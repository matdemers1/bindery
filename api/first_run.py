"""Claiming a fresh install in the browser (Phase 19, T-19.4 to T-19.6).

A new Bindery has no accounts, and since ADR-008 it may already be reachable
from the internet through the tunnel. "Whoever opens the URL first owns the
archive" would hand a stranger somebody's discharge papers, so the claim needs a
secret that only the host's operator can read: a **setup code**, printed to the
api container's stdout while `app_user` is empty.

Four properties hold the design up, and each is a specific piece below:

- **The code is never stored and never logged.** Only a keyed hash sits in
  `setting`. It is printed with `print(..., flush=True)` rather than through
  `logging`, because `api/eventlog.py` copies every `bindery.*` log line into
  `event_log`, which the diagnostics screen reads — a secret should not sit in a
  table, even an expired one. Anyone who can read the container's stdout already
  controls the host.
- **One claim, ever.** The claim is checked against an empty `app_user` in the
  same transaction as the insert, under a transaction-scoped advisory lock, so
  two racing requests cannot both see an empty table.
- **The hash is cleared at claim**, not merely left unusable.
- **Administrator rights go through `accounts.grant_admin`**, which refuses
  without TOTP (REQ-156). Setup goes through that rule, never around it: the
  claimed account is recorded as `setup_owner_user_id`, enrols a second factor
  with the ordinary endpoints, and only then completes.
"""

import asyncio
import hashlib
import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api import accounts
from api.audit import record
from api.auth.passwords import hash_password, validate_password
from api.config import get_settings
from api.db.enums import ActorType, LibraryKind, MembershipRole
from api.db.models import AppUser, Library, Membership, Setting

log = logging.getLogger("bindery.setup")

# No 0/O, no 1/I/L: this gets copied off a terminal by eye.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 12
GROUP = 4

# Every boot mints a fresh one, so this only matters to an install left
# unclaimed for a long time. `api.cli setup-code` prints a new one on demand.
CODE_TTL = timedelta(hours=24)

# Setting keys. Deliberately absent from `settings_store.WRITABLE`: nothing on
# the settings screen may read or write them.
CODE_HASH = "setup_code_hash"
CODE_CREATED_AT = "setup_code_created_at"
OWNER_USER_ID = "setup_owner_user_id"
# What audit rows about the setup settings point at.
SETTING_ENTITY_ID = uuid.uuid5(uuid.NAMESPACE_URL, "bindery:setting:setup")

# One message for a wrong, missing, malformed or expired code (REQ-135's posture).
INVALID_CODE = "that setup code is not valid"

# The advisory-lock key every claim, CLI create and code issue serialises on.
# Arbitrary, fixed, and distinct from the throttle's per-address keys, which are
# hashes of addresses.
_LOCK_KEY = int.from_bytes(hashlib.sha256(b"bindery:first-run").digest()[:8], "big", signed=True)

# How long the boot task waits before asking again when the database is not
# ready — typically a fresh install whose migrations have not been applied yet.
BOOT_RETRY_SECONDS = 15.0


class SetupState(StrEnum):
    UNCLAIMED = "unclaimed"
    NEEDS_SECOND_FACTOR = "needs_second_factor"
    COMPLETE = "complete"


class SetupError(Exception):
    """Refused. `kind` says which answer the route should give."""

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(message)


class AlreadyClaimed(SetupError):
    def __init__(self) -> None:
        super().__init__("claimed", "this archive has already been claimed")


class InvalidCode(SetupError):
    def __init__(self) -> None:
        super().__init__("invalid_code", INVALID_CODE)


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------
# The code itself
# --------------------------------------------------------------------------


def generate_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def display(code: str) -> str:
    """`ABCDEFGHJKMN` → `ABCD-EFGH-JKMN`."""
    return "-".join(code[i : i + GROUP] for i in range(0, len(code), GROUP))


def normalise(typed: str | None) -> str | None:
    """Case-insensitive, dashes and whitespace ignored. None if it cannot be a code."""
    cleaned = "".join(ch for ch in (typed or "").upper() if ch not in "- \t\r\n")
    if len(cleaned) != CODE_LENGTH or any(ch not in ALPHABET for ch in cleaned):
        return None
    return cleaned


def _digest(code: str) -> str:
    """HMAC-SHA256 keyed on JWT_SECRET, not a bare hash.

    Twelve characters of a 31-letter alphabet is about 59 bits — ample against
    a throttled guesser, thinner against a leaked database dump and a GPU. The
    key lives in the environment, not the dump, so the dump alone is not enough.
    """
    key = hashlib.sha256(b"bindery-setup-code:" + get_settings().jwt_secret.encode()).digest()
    return hmac.new(key, code.encode(), hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------
# Settings rows — read and written here only
# --------------------------------------------------------------------------


async def _read(session: AsyncSession, key: str) -> str | None:
    return (
        await session.execute(sa.select(Setting.value).where(Setting.key == key))
    ).scalar_one_or_none()


async def _write(session: AsyncSession, key: str, value: str | None) -> None:
    """Upsert. Clearing sets NULL rather than removing the row (REQ-090)."""
    await session.execute(
        insert(Setting)
        .values(key=key, value=value, is_secret=False)
        .on_conflict_do_update(
            index_elements=[Setting.key],
            set_={"value": value, "updated_at": sa.func.now()},
        )
    )


async def _clear_code(session: AsyncSession) -> bool:
    """NULL the code settings. True if there was anything to clear."""
    cleared = False
    for key in (CODE_HASH, CODE_CREATED_AT):
        if await _read(session, key) is not None:
            await _write(session, key, None)
            cleared = True
    return cleared


async def _audit_system(session: AsyncSession, action: str, **after: object) -> None:
    """Audit a first-run settings change nobody signed in made.

    `entity_id` is a fixed id for the setting rather than a user, since at boot
    there is no user. Never the code, never its hash.
    """
    await record(
        session, entity_type="setting", entity_id=SETTING_ENTITY_ID, action=action,
        actor_type=ActorType.SYSTEM, after=dict(after) or None,
    )


async def lock(session: AsyncSession) -> None:
    """Serialise on the first-run lock until this transaction ends."""
    await session.execute(sa.select(sa.func.pg_advisory_xact_lock(_LOCK_KEY)))


async def archive_is_empty(session: AsyncSession) -> bool:
    return not (
        await session.execute(sa.select(sa.exists().where(AppUser.id.is_not(None))))
    ).scalar_one()


async def _admin_exists(session: AsyncSession) -> bool:
    return (
        await session.execute(sa.select(sa.exists().where(AppUser.is_admin.is_(True))))
    ).scalar_one()


async def owner_id(session: AsyncSession) -> uuid.UUID | None:
    raw = await _read(session, OWNER_USER_ID)
    try:
        return uuid.UUID(raw) if raw else None
    except ValueError:
        return None


async def resume_at_second_factor(session: AsyncSession, *, user: AppUser) -> bool:
    """Arm the Secure step for an owner who has just lost their authenticator.

    Only when no administrator is left: with another admin still holding the box, the way back
    is that admin re-granting rights once this account has enrolled again from Settings, and
    recording a setup owner would claim a setup is in progress that nobody is doing.
    """
    if await _admin_exists(session):
        return False
    await _write(session, OWNER_USER_ID, str(user.id))
    await _audit_system(
        session, "setup_owner_recorded", reason="second factor retired; resume at Secure"
    )
    return True


async def state(session: AsyncSession) -> SetupState:
    """The one fact an anonymous caller may learn: is this archive claimed?"""
    if await archive_is_empty(session):
        return SetupState.UNCLAIMED
    if await _admin_exists(session):
        return SetupState.COMPLETE
    if await owner_id(session) is not None:
        return SetupState.NEEDS_SECOND_FACTOR
    # Accounts, no administrator and no recorded owner: an install older than
    # this phase whose admin was never promoted. The browser claim is closed
    # (claim answers 409), so "unclaimed" would be a lie; there is no setup to
    # resume either. Complete is the only answer that sends nobody down a dead end.
    return SetupState.COMPLETE


# --------------------------------------------------------------------------
# Issuing a code
# --------------------------------------------------------------------------


def announce(code: str) -> None:
    """The banner. stdout only — never `logging`, which persists to event_log."""
    prefix = "[bindery setup]"
    lines = [
        "=" * 64,
        "This archive has not been claimed yet.",
        "Open Bindery in a browser and enter this setup code:",
        "",
        f"    {display(code)}",
        "",
        f"It works once and expires in {int(CODE_TTL.total_seconds() // 3600)} hours.",
        "Print a new one with:  docker compose exec api python -m api.cli setup-code",
        "=" * 64,
    ]
    print("\n".join(f"{prefix} {line}".rstrip() for line in lines), flush=True)


async def issue_code(session: AsyncSession) -> str | None:
    """Mint and store a fresh code if the archive is empty. Returns it, or None.

    The caller commits. Any code issued before is replaced, so only the most
    recently printed one works.
    """
    await lock(session)
    if not await archive_is_empty(session):
        return None
    code = generate_code()
    await _write(session, CODE_HASH, _digest(code))
    await _write(session, CODE_CREATED_AT, _now().isoformat())
    await _audit_system(
        session, "setup_code_issued", expires_in_hours=CODE_TTL // timedelta(hours=1)
    )
    return code


async def ensure_setup_code(session: AsyncSession) -> str | None:
    """Boot-time: print a fresh code while unclaimed, tidy up once claimed.

    Commits. Returns the code (for tests) or None when the archive has accounts.
    """
    code = await issue_code(session)
    if code is None:
        if await _clear_code(session):
            await _audit_system(session, "setup_code_cleared", reason="archive has accounts")
        if await _admin_exists(session) and await owner_id(session) is not None:
            await _write(session, OWNER_USER_ID, None)
            await _audit_system(session, "setup_owner_cleared", reason="an administrator exists")
        await session.commit()
        return None
    await session.commit()
    announce(code)
    return code


async def announce_on_boot(stopping: asyncio.Event, session_factory) -> None:
    """The lifespan task. Never raises, and never blocks the api from serving.

    A fresh install's first boot is usually *before* `make migrate`, so the
    tables may not exist yet. Rather than fail — or print nothing until a
    restart nobody knows to do — it asks again every few seconds until the
    question has an answer, then stops.
    """
    warned = False
    while not stopping.is_set():
        try:
            async with session_factory() as session:
                await ensure_setup_code(session)
            return
        except Exception as error:
            if not warned:
                # The error type only: the message could in principle carry a
                # parameter, and this path handles a secret.
                log.warning(
                    "could not check whether this archive has been claimed yet "
                    "(%s) — have migrations been applied? Retrying.",
                    type(error).__name__,
                )
                warned = True
        try:
            await asyncio.wait_for(stopping.wait(), timeout=BOOT_RETRY_SECONDS)
        except TimeoutError:
            pass


# --------------------------------------------------------------------------
# Claim and complete
# --------------------------------------------------------------------------


@dataclass
class Claimed:
    user: AppUser
    library: Library


async def verify_code(session: AsyncSession, typed: str | None) -> None:
    """Raise `InvalidCode` for a wrong, malformed, missing or expired code."""
    stored = await _read(session, CODE_HASH)
    created_raw = await _read(session, CODE_CREATED_AT)
    candidate = normalise(typed)
    if stored is None or created_raw is None or candidate is None:
        raise InvalidCode()
    try:
        created = datetime.fromisoformat(created_raw)
    except ValueError as error:
        raise InvalidCode() from error
    if created + CODE_TTL <= _now():
        raise InvalidCode()
    if not hmac.compare_digest(stored, _digest(candidate)):
        raise InvalidCode()


async def claim(
    session: AsyncSession,
    *,
    code: str,
    email: str,
    password: str,
    display_name: str | None,
    library_name: str,
) -> Claimed:
    """Create the first account, its library and its ownership. Caller commits.

    Raises `AlreadyClaimed`, `WeakPassword`, `accounts.AccountError` (a bad
    email or library name) or `InvalidCode`, in that order — so a wrong code is
    only ever reported to someone whose password would have been accepted, and
    nothing about the code is learned from a 422.
    """
    await lock(session)
    if not await archive_is_empty(session):
        raise AlreadyClaimed()

    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise accounts.AccountError("that does not look like an email address")
    library_name = (library_name or "").strip()
    if not library_name:
        raise accounts.AccountError("name the first library")
    validate_password(password, email=email)

    await verify_code(session, code)

    user = AppUser(
        email=email,
        password_hash=hash_password(password),
        display_name=(display_name or "").strip() or None,
    )
    # Personal, like every account an invitation or `create-user` makes: the
    # owner's first library is theirs alone (ADR-009). Shared libraries are a
    # household decision made later, from the household screen.
    library = Library(name=library_name, kind=LibraryKind.PERSONAL)
    session.add_all([user, library])
    await session.flush()
    session.add(Membership(user_id=user.id, library_id=library.id, role=MembershipRole.OWNER))

    await _write(session, OWNER_USER_ID, str(user.id))
    await _clear_code(session)
    await session.flush()
    log.info("archive claimed in the browser")
    return Claimed(user=user, library=library)


async def record_cli_owner(session: AsyncSession, user: AppUser) -> None:
    """`create-user` on an empty archive: the same resume-at-Secure path.

    The caller must have taken `lock` and seen an empty archive *before*
    inserting the user.
    """
    await _write(session, OWNER_USER_ID, str(user.id))
    await _clear_code(session)


async def complete(session: AsyncSession, *, user: AppUser) -> None:
    """Grant the setup owner administrator rights. Caller audits and commits.

    Raises `SetupError` with kind `complete` (an administrator already exists),
    `no_setup` (nothing to finish), `not_owner`, or `accounts.AccountError` from
    `grant_admin` when TOTP is not enrolled.
    """
    await lock(session)
    if await _admin_exists(session):
        raise SetupError("complete", "setup is already complete")
    owner = await owner_id(session)
    if owner is None:
        raise SetupError("no_setup", "there is no setup in progress")
    if owner != user.id:
        raise SetupError(
            "not_owner", "only the account that claimed this archive can finish setting it up"
        )
    await accounts.grant_admin(session, user=user)
    await _write(session, OWNER_USER_ID, None)
