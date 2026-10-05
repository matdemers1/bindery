"""Deleting your own account: the request, the grace period, the way back (BND-T-23.3).

BND-ADR-015 is the decision; this module is its non-destructive half. Asking disables the account
at once and ends everything it was signed in with, then waits. Nothing here removes a row — that
is `api/account_purge.py`, the one other file REQ-090 lets delete, and it runs only once the grace
period this module sets has passed without an administrator restoring the account.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.accounts import revoke_other_sessions
from api.db.enums import MembershipRole
from api.db.models import ApiToken, AppUser, Library, Membership, RelayRegistration

# The contract asks for at least a day. A week, so that a regret on Monday can still be undone by
# an administrator restoring the account.
GRACE = timedelta(days=7)


@dataclass(frozen=True)
class LastOwner:
    """Why this account cannot go yet, in words for the person asking."""

    detail: str


@dataclass(frozen=True)
class Scheduled:
    grace_until: datetime
    sessions_ended: int
    api_tokens_revoked: int
    push_forgotten: int


def _standing():
    """Accounts that still count as somebody: not disabled, not on their way out."""
    return sa.and_(
        AppUser.is_active.is_(True),
        AppUser.delete_after.is_(None),
        AppUser.deleted_at.is_(None),
    )


async def last_owner(session: AsyncSession, user: AppUser) -> LastOwner | None:
    """The reason this account is the last of something, or None.

    Two things cannot be left without anyone: the instance's administrators (an archive with no
    administrator can never appoint one) and the owners of a library somebody else belongs to (a
    library with no owner is one nobody can ever fix — `set_member_role` refuses it for the same
    reason). Accounts already scheduled for deletion are not the "somebody else".
    """
    if user.is_admin:
        others = await session.scalar(
            sa.select(sa.func.count())
            .select_from(AppUser)
            .where(AppUser.is_admin.is_(True), AppUser.id != user.id, _standing())
        )
        if not others:
            return LastOwner(
                "You're the only administrator. Make someone else an administrator in "
                "Bindery first, then delete this account."
            )

    owned = (
        await session.execute(
            sa.select(Library.id, Library.name)
            .join(Membership, Membership.library_id == Library.id)
            .where(Membership.user_id == user.id, Membership.role == MembershipRole.OWNER)
            .order_by(Library.name)
        )
    ).all()
    for library_id, name in owned:
        members = sa.select(Membership).join(AppUser, AppUser.id == Membership.user_id).where(
            Membership.library_id == library_id, Membership.user_id != user.id, _standing()
        )
        has_others = await session.scalar(sa.select(sa.exists(members)))
        if not has_others:
            continue  # private: it goes with the account (BND-ADR-015)
        has_owner = await session.scalar(
            sa.select(sa.exists(members.where(Membership.role == MembershipRole.OWNER)))
        )
        if not has_owner:
            return LastOwner(
                f"You're the only owner of the library “{name}”, which other people "
                "use. Make one of them an owner first, then delete this account."
            )
    return None


async def schedule(
    session: AsyncSession, user: AppUser, *, now: datetime | None = None
) -> Scheduled:
    """Disable the account now and set when the purge may remove it.

    Everything it could still act through ends here, not at the purge: every session, native and
    browser; every API token; every push registration. The caller has already proved the person
    with a current code and checked `last_owner`.
    """
    now = now or datetime.now(UTC)
    user.is_active = False
    user.suspended_at = now
    user.suspended_by_id = user.id
    user.delete_after = now + GRACE

    sessions_ended = await revoke_other_sessions(session, user)
    tokens = (
        await session.execute(
            sa.update(ApiToken)
            .where(ApiToken.user_id == user.id, ApiToken.revoked_at.is_(None))
            .values(revoked_at=now)
            .returning(ApiToken.id)
        )
    ).scalars().all()
    forgotten = (
        await session.execute(
            sa.update(RelayRegistration)
            .where(RelayRegistration.user_id == user.id, RelayRegistration.forgotten_at.is_(None))
            .values(forgotten_at=now)
            .returning(RelayRegistration.id)
        )
    ).scalars().all()
    await session.flush()
    return Scheduled(
        grace_until=now + GRACE,
        sessions_ended=sessions_ended,
        api_tokens_revoked=len(tokens),
        push_forgotten=len(forgotten),
    )


def is_pending(user: AppUser) -> bool:
    return user.delete_after is not None and user.deleted_at is None


async def due(session: AsyncSession, now: datetime) -> list[uuid.UUID]:
    """Accounts whose grace period has passed and which nobody restored."""
    return list(
        (
            await session.execute(
                sa.select(AppUser.id)
                .where(
                    AppUser.delete_after.is_not(None),
                    AppUser.delete_after <= now,
                    AppUser.deleted_at.is_(None),
                    AppUser.is_active.is_(False),
                )
                .order_by(AppUser.delete_after)
            )
        ).scalars()
    )
