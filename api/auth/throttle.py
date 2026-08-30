"""Login throttling and lockout (T-10.1, T-10.2 · REQ-133, REQ-134).

Bindery's login page is about to become the front door — Cloudflare Access is
being removed (ADR-008), so this is what stands between the open internet and an
archive of discharge papers, medical records and mortgages.

Two counters, because they defend against different things and a single one gets
one of them wrong:

**Per IP** stops the ordinary case: one host walking a password list. The cost
grows with each failure, so a slow attacker is merely slow and a fast one stops
being fast.

**Per account** stops a distributed attempt at one known address. It is
deliberately far looser than the IP limit, because a per-account lockout is a
weapon: anyone who knows the address can lock the owner out of their own archive
by failing to log in as them. So the account limit is high, the lockout is short
and self-expiring, and an administrator can clear it.

State lives in Postgres like everything else here. There is no Redis to run, and
a login attempt is a fact worth keeping anyway — `event_log` and this table are
what answer "was anyone trying?" after the fact.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import AppUser, LoginAttempt

log = logging.getLogger("bindery.auth")

# How far back a failure still counts against you.
WINDOW = timedelta(minutes=15)

# Failures from one address before the cost starts. Below this, a person
# mistyping their own password notices nothing.
IP_FREE_ATTEMPTS = 4
# The delay after that doubles: 2s, 4s, 8s … up to the cap. Ten failures costs
# about a minute of waiting; a hundred costs the better part of an hour.
IP_BACKOFF_BASE = timedelta(seconds=2)
IP_BACKOFF_CAP = timedelta(minutes=10)

# Failures against one address, from anywhere, before it is locked. High on
# purpose — see the module docstring.
ACCOUNT_MAX_ATTEMPTS = 25
ACCOUNT_LOCKOUT = timedelta(minutes=15)


class Throttled(Exception):
    """Refused for now, and told how long for.

    Carries `retry_after` so the response can say it. A throttle that gives no
    number reads as a broken login and generates a support conversation.
    """

    def __init__(self, retry_after: timedelta, reason: str) -> None:
        self.retry_after = max(1, int(retry_after.total_seconds()))
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class AttemptState:
    ip_failures: int
    account_failures: int
    locked_until: datetime | None


def _now() -> datetime:
    return datetime.now(UTC)


def _ip_backoff(failures: int) -> timedelta:
    if failures <= IP_FREE_ATTEMPTS:
        return timedelta(0)
    # Doubling, capped. `min` on the exponent first so the shift cannot get
    # silly on a very long-running attack.
    steps = min(failures - IP_FREE_ATTEMPTS, 20)
    return min(IP_BACKOFF_BASE * (2 ** (steps - 1)), IP_BACKOFF_CAP)


async def check(session: AsyncSession, email: str, ip: str | None) -> None:
    """Raise `Throttled` if this attempt should not be tried at all.

    Called before the password is verified, so a throttled request costs no
    Argon2 work — which is the point, since Argon2 is deliberately expensive and
    would otherwise be the denial-of-service.
    """
    since = _now() - WINDOW
    email = email.strip().lower()

    user = (
        await session.execute(sa.select(AppUser).where(AppUser.email == email))
    ).scalar_one_or_none()
    if user is not None and user.locked_until and user.locked_until > _now():
        raise Throttled(user.locked_until - _now(), "account temporarily locked")

    if ip:
        row = (
            await session.execute(
                sa.select(
                    sa.func.count(), sa.func.max(LoginAttempt.created_at)
                ).where(
                    LoginAttempt.ip == ip,
                    LoginAttempt.succeeded.is_(False),
                    LoginAttempt.created_at > since,
                )
            )
        ).one()
        failures, last_failure = int(row[0] or 0), row[1]
        backoff = _ip_backoff(failures)
        if backoff and last_failure is not None:
            ready_at = last_failure + backoff
            if ready_at > _now():
                raise Throttled(ready_at - _now(), "too many attempts from this address")


async def record(
    session: AsyncSession, email: str, ip: str | None, *, succeeded: bool
) -> None:
    """Write the attempt down, and lock the account if it has had enough.

    Committed by the caller in the same transaction as everything else it did,
    so a failure that rolls back is not counted — an attempt that never reached
    the password check is not an attempt against the password.
    """
    email = email.strip().lower()
    session.add(LoginAttempt(email=email, ip=ip, succeeded=succeeded))

    user = (
        await session.execute(sa.select(AppUser).where(AppUser.email == email))
    ).scalar_one_or_none()

    if succeeded:
        if user is not None and user.locked_until:
            user.locked_until = None
        # Not the address. `login_attempt` holds it and is reachable only by an
        # administrator; this line is globally visible (REQ-144).
        log.info("login succeeded from %s", ip or "an unknown address")
        return

    # Logged at warning, not error: a mistyped password is not an incident, and
    # an error filter full of them is an error filter nobody reads.
    log.warning("login failed from %s", ip or "an unknown address")

    if user is None:
        return

    since = _now() - WINDOW
    failures = int(
        (
            await session.execute(
                sa.select(sa.func.count()).where(
                    LoginAttempt.email == email,
                    LoginAttempt.succeeded.is_(False),
                    LoginAttempt.created_at > since,
                )
            )
        ).scalar_one()
        or 0
    )
    # +1 for the row just added, which has not been flushed yet.
    if failures + 1 >= ACCOUNT_MAX_ATTEMPTS:
        user.locked_until = _now() + ACCOUNT_LOCKOUT
        log.error(
            "an account was locked until %s after %s failed attempts",
            user.locked_until.isoformat(timespec="seconds"), failures + 1,
        )
