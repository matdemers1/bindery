"""Rate-limiting the vault unlock (ADR-012).

The PIN path counts its own failures and destroys the PIN-wrapped copy of the
data key after five — `api/vault/service.py` explains why removing the material
beats locking a counter somebody with the database can reset.

The passphrase path had nothing. No counter, no lockout, no delay: the route
returned 401 and committed, and the caller tried again. That is the wrong way
round. The passphrase is the *durable* secret — there is no reset, and losing it
loses the documents — so it is the one credential in the design that must never
have unlimited online guesses, and it was the only one that did.

The same treatment as the PIN is not available and would be wrong: destroying
the passphrase wrapper destroys the vault, so the answer here has to be a
refusal that expires rather than a removal that does not. That is exactly the
shape of `api/auth/throttle.py`, which is why this reuses it rather than growing
a second limiter beside it:

- **Per address**, through `throttle.check`, which brings the doubling backoff
  and the advisory lock that makes a burst queue instead of racing.
- **Per vault**, counted here, because the address half alone does nothing about
  an attacker with more than one of them. Deliberately a temporary refusal with
  no destructive step and no administrator needed to clear it.

Attempts are written to `login_attempt` under a synthetic key, exactly as
`api/routers/accounts.py` does for invitations and reset codes. They are
authentication attempts, and "was anyone trying?" is as much worth answering
about this door as about the front one.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import throttle
from api.db.models import LoginAttempt

log = logging.getLogger("bindery.vault")

# Higher than the PIN's five, because a wrong passphrase is easy to mistype and
# the consequence of running out is a wait rather than the loss of a wrapper.
# Far below the number of guesses worth making against twelve characters.
MAX_ATTEMPTS = 10

# Self-expiring, like the account lockout and for the same reason: a limit that
# needs an administrator to clear is a limit anyone who knows the address can
# use to lock the owner out of their own vault.
COOLDOWN = timedelta(minutes=15)


def attempt_key(user_id: uuid.UUID) -> str:
    """The `login_attempt.email` these are recorded under.

    Namespaced so a vault attempt can never be confused with, or counted
    against, the account's own login attempts — and so no real address can
    collide with it.
    """
    return f"vault:{user_id}"


async def check(session: AsyncSession, user_id: uuid.UUID, ip: str | None) -> None:
    """Raise `throttle.Throttled` if this unlock should not be attempted at all.

    Called before any derivation, so a refused attempt costs no Argon2 — which
    matters more here than at the login form, because the vault parameters are
    256 MiB and about a second each.
    """
    # The per-address half, with its backoff and its advisory lock.
    await throttle.check(session, attempt_key(user_id), ip)

    since = datetime.now(UTC) - COOLDOWN
    row = (
        await session.execute(
            sa.select(sa.func.count(), sa.func.max(LoginAttempt.created_at)).where(
                LoginAttempt.email == attempt_key(user_id),
                LoginAttempt.succeeded.is_(False),
                LoginAttempt.created_at > since,
            )
        )
    ).one()
    failures, last_failure = int(row[0] or 0), row[1]

    if failures >= MAX_ATTEMPTS and last_failure is not None:
        ready_at = last_failure + COOLDOWN
        if ready_at > datetime.now(UTC):
            raise throttle.Throttled(
                ready_at - datetime.now(UTC), "too many attempts against this vault"
            )


async def record(
    session: AsyncSession, user_id: uuid.UUID, ip: str | None, *, succeeded: bool
) -> None:
    """Write the attempt down. Committed by the caller, like the login form's.

    The row goes straight into `login_attempt` rather than through
    `throttle.record`, which would also try to lock an account named
    `vault:<uuid>` and would log the words "login succeeded". The row is the
    same shape either way, so `throttle.check`'s per-address count sees it and
    an unlock attempt costs the same backoff as a login attempt from that
    address — which is the point.
    """
    session.add(
        LoginAttempt(email=attempt_key(user_id), ip=ip, succeeded=succeeded)
    )
    if not succeeded:
        # Warning, not error: a mistyped passphrase is not an incident, and an
        # error filter full of them is one nobody reads.
        log.warning("vault unlock failed from %s", ip or "an unknown address")
