"""Accounts: invitations, resets, TOTP, suspension (Phase 10b).

Everything here is shaped by two decisions. There is **no email service**
(ADR-008), so invitations and reset codes are handed over out of band and can
never be re-read from the database. And an administrator administers *accounts*
(ADR-009), so no test here may find a way for one to set another user's
password, read their documents, or assume their session — several of these
tests exist to assert the absence of exactly that.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from api import accounts
from api.auth import totp
from api.auth.passwords import WeakPassword, verify_password
from api.db.models import Invitation, Membership, PasswordResetCode, RefreshToken

GOOD_PASSWORD = "ledger obelisk hangar 41"


@pytest.fixture
async def admin(session, signed_in):
    user, _library = await signed_in()
    user.is_admin = True
    await session.commit()
    return user


# --------------------------------------------------------------------------
# Invitations
# --------------------------------------------------------------------------


async def test_an_invitation_creates_one_account_and_one_library(
    session, admin
) -> None:
    issued = await accounts.invite(
        session, email="Sister@Example.com ", library_name="Sister's Papers",
        created_by=admin, storage_quota_bytes=5_000_000_000,
    )
    await session.commit()

    user = await accounts.accept(
        session, token=issued.token, password=GOOD_PASSWORD, display_name="Sister"
    )
    await session.commit()

    assert user.email == "sister@example.com", "normalised on the way in"
    assert user.storage_quota_bytes == 5_000_000_000, "the quota came from the invite"
    assert user.is_admin is False

    memberships = (
        await session.execute(
            sa.select(Membership).where(Membership.user_id == user.id)
        )
    ).scalars().all()
    assert len(memberships) == 1, "one library, and no access to anyone else's"
    assert memberships[0].role.value == "owner"


async def test_an_invitation_is_single_use(session, admin) -> None:
    issued = await accounts.invite(
        session, email="once@example.com", library_name="Papers", created_by=admin
    )
    await session.commit()
    await accounts.accept(session, token=issued.token, password=GOOD_PASSWORD, display_name=None)
    await session.commit()

    with pytest.raises(accounts.AccountError, match="already been used"):
        await accounts.accept(
            session, token=issued.token, password=GOOD_PASSWORD, display_name=None
        )


async def test_an_expired_invitation_is_refused(session, admin) -> None:
    issued = await accounts.invite(
        session, email="late@example.com", library_name="Papers", created_by=admin
    )
    issued.invitation.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    with pytest.raises(accounts.AccountError, match="expired"):
        await accounts.find_invitation(session, issued.token)


async def test_a_withdrawn_invitation_is_refused(session, admin) -> None:
    issued = await accounts.invite(
        session, email="gone@example.com", library_name="Papers", created_by=admin
    )
    issued.invitation.revoked_at = datetime.now(UTC)
    await session.commit()

    with pytest.raises(accounts.AccountError, match="withdrawn"):
        await accounts.find_invitation(session, issued.token)


async def test_the_invitation_token_is_never_stored(session, admin) -> None:
    """It is handed over by text message and cannot be recovered afterwards —
    the same contract as an API token."""
    issued = await accounts.invite(
        session, email="secret@example.com", library_name="Papers", created_by=admin
    )
    await session.commit()

    stored = (
        await session.execute(sa.select(Invitation).where(Invitation.id == issued.invitation.id))
    ).scalar_one()
    assert issued.token not in stored.token_hash
    assert len(stored.token_hash) == 64, "a sha-256 hex digest"


async def test_an_invitation_will_not_shadow_an_existing_account(session, admin) -> None:
    with pytest.raises(accounts.AccountError, match="already exists"):
        await accounts.invite(
            session, email=admin.email, library_name="Papers", created_by=admin
        )


async def test_accepting_enforces_the_password_policy(session, admin) -> None:
    issued = await accounts.invite(
        session, email="weak@example.com", library_name="Papers", created_by=admin
    )
    await session.commit()

    with pytest.raises(WeakPassword):
        await accounts.accept(
            session, token=issued.token, password="short", display_name=None
        )


# --------------------------------------------------------------------------
# Password reset — the admin opens the door and does not walk through it
# --------------------------------------------------------------------------


async def test_a_reset_code_lets_the_holder_choose_their_own_password(
    session, admin, user_factory
) -> None:
    user, _ = await user_factory()
    code = await accounts.issue_reset_code(session, user=user, issued_by=admin)
    await session.commit()

    await accounts.redeem_reset_code(
        session, email=user.email, code=code, new_password=GOOD_PASSWORD
    )
    await session.commit()
    await session.refresh(user)

    assert verify_password(user.password_hash, GOOD_PASSWORD)


async def test_no_code_path_lets_an_admin_set_another_password(session) -> None:
    """ADR-009's sharpest edge, asserted rather than described.

    An administrator can open the door. If they could also walk through it,
    they would afterwards be unable to prove they had not.
    """
    import inspect

    source = inspect.getsource(accounts)
    assert "def set_password" not in source
    # `change_password` is self-service: it takes the user whose password it is.
    signature = inspect.signature(accounts.change_password)
    assert set(signature.parameters) == {"session", "user", "new_password"}


async def test_a_reset_code_is_single_use(session, admin, user_factory) -> None:
    user, _ = await user_factory()
    code = await accounts.issue_reset_code(session, user=user, issued_by=admin)
    await session.commit()
    await accounts.redeem_reset_code(
        session, email=user.email, code=code, new_password=GOOD_PASSWORD
    )
    await session.commit()

    with pytest.raises(accounts.AccountError, match="not valid"):
        await accounts.redeem_reset_code(
            session, email=user.email, code=code, new_password="another good one 99"
        )


async def test_an_expired_reset_code_is_refused(session, admin, user_factory) -> None:
    user, _ = await user_factory()
    code = await accounts.issue_reset_code(session, user=user, issued_by=admin)
    await session.commit()
    await session.execute(
        sa.update(PasswordResetCode).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()

    with pytest.raises(accounts.AccountError, match="not valid"):
        await accounts.redeem_reset_code(
            session, email=user.email, code=code, new_password=GOOD_PASSWORD
        )


async def test_one_message_for_every_way_a_reset_can_fail(
    session, admin, user_factory
) -> None:
    """Otherwise the reset form reports whether an address exists, which is the
    same oracle the login form was carefully denied (REQ-135)."""
    user, _ = await user_factory()
    code = await accounts.issue_reset_code(session, user=user, issued_by=admin)
    await session.commit()

    messages = set()
    for email, sent in [
        ("nobody@example.com", code),
        (user.email, "AAAA-BBBB-CCCC"),
    ]:
        with pytest.raises(accounts.AccountError) as raised:
            await accounts.redeem_reset_code(
                session, email=email, code=sent, new_password=GOOD_PASSWORD
            )
        messages.add(str(raised.value))
    assert len(messages) == 1, messages


async def test_a_reset_ends_every_existing_session(session, admin, user_factory) -> None:
    """The usual reason for a reset is that someone else may have the old one."""
    user, _ = await user_factory()
    session.add_all([
        RefreshToken(
            user_id=user.id, token_hash=uuid.uuid4().hex,
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        for _ in range(3)
    ])
    code = await accounts.issue_reset_code(session, user=user, issued_by=admin)
    await session.commit()

    await accounts.redeem_reset_code(
        session, email=user.email, code=code, new_password=GOOD_PASSWORD
    )
    await session.commit()

    live = (
        await session.execute(
            sa.select(sa.func.count()).select_from(RefreshToken).where(
                RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
            )
        )
    ).scalar_one()
    assert live == 0


async def test_changing_a_password_ends_every_other_session(session, user_factory) -> None:
    user, _ = await user_factory()
    session.add_all([
        RefreshToken(
            user_id=user.id, token_hash=uuid.uuid4().hex,
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        for _ in range(2)
    ])
    await session.commit()

    await accounts.change_password(session, user=user, new_password=GOOD_PASSWORD)
    await session.commit()

    live = (
        await session.execute(
            sa.select(sa.func.count()).select_from(RefreshToken).where(
                RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
            )
        )
    ).scalar_one()
    assert live == 0


# --------------------------------------------------------------------------
# TOTP
# --------------------------------------------------------------------------


async def test_a_secret_is_not_trusted_until_a_code_proves_it(
    session, user_factory
) -> None:
    """Storing it as active before enrolment is confirmed locks the account
    holder out of their own archive when the enrolment did not take."""
    user, _ = await user_factory()
    user.totp_secret = totp.new_secret()
    await session.commit()

    assert user.totp_enabled is False

    with pytest.raises(accounts.AccountError, match="not right"):
        await accounts.confirm_totp(session, user=user, code="000000")
    assert user.totp_enabled is False

    good = totp._code_for_step(user.totp_secret, totp.current_step())
    codes = await accounts.confirm_totp(session, user=user, code=good)
    await session.commit()

    assert user.totp_enabled is True
    assert len(codes) == accounts.RECOVERY_CODE_COUNT


async def test_starting_an_enrolment_cannot_disable_a_live_second_factor(
    client, session, signed_in
) -> None:
    """The factor that stops a stolen password must not come off with a session.

    Starting an enrolment used to clear `totp_confirmed_at`, so one POST — no
    password, no code — left the account on a password alone, and did it past
    the refusal in `totp_disable` that exists so an administrator never is.
    """
    user, _ = await signed_in()
    user.totp_secret = totp.new_secret()
    await accounts.confirm_totp(
        session, user=user,
        code=totp._code_for_step(user.totp_secret, totp.current_step()),
    )
    await session.commit()
    assert user.totp_enabled is True

    response = await client.post("/api/account/totp/start")
    assert response.status_code == 409, response.text

    await session.refresh(user)
    assert user.totp_enabled is True
    assert (await client.get("/api/account")).json()["totp_enabled"] is True


async def test_an_account_with_no_second_factor_can_still_enrol(
    client, session, signed_in
) -> None:
    """The refusal above is about replacing a live factor, not about enrolling."""
    user, _ = await signed_in()
    started = await client.post("/api/account/totp/start")
    assert started.status_code == 200, started.text

    confirmed = await client.post("/api/account/totp/confirm", json={
        "code": totp._code_for_step(started.json()["secret"], totp.current_step())
    })
    assert confirmed.status_code == 200, confirmed.text

    await session.refresh(user)
    assert user.totp_enabled is True


async def test_the_same_totp_code_cannot_be_used_twice(session, user_factory) -> None:
    """Without the replay guard a code stays valid across the whole window —
    up to ninety seconds, which is ample to reuse one that was captured."""
    user, _ = await user_factory()
    user.totp_secret = totp.new_secret()
    code = totp._code_for_step(user.totp_secret, totp.current_step())
    await accounts.confirm_totp(session, user=user, code=code)
    await session.commit()

    with pytest.raises(accounts.AccountError, match="not right"):
        await accounts.check_second_factor(session, user=user, code=code)


async def test_a_recovery_code_works_once(session, user_factory) -> None:
    user, _ = await user_factory()
    user.totp_secret = totp.new_secret()
    codes = await accounts.confirm_totp(
        session, user=user, code=totp._code_for_step(user.totp_secret, totp.current_step())
    )
    await session.commit()

    await accounts.check_second_factor(session, user=user, code=codes[0])
    await session.commit()

    with pytest.raises(accounts.AccountError):
        await accounts.check_second_factor(session, user=user, code=codes[0])


async def test_a_clock_a_step_out_still_works(session, user_factory) -> None:
    """People type slowly and phones drift."""
    _user, _ = await user_factory()
    secret = totp.new_secret()
    previous = totp._code_for_step(secret, totp.current_step() - 1)
    assert totp.verify(secret, previous) is not None

    too_old = totp._code_for_step(secret, totp.current_step() - 5)
    assert totp.verify(secret, too_old) is None


async def test_admin_rights_are_refused_without_two_factor(
    session, user_factory
) -> None:
    """REQ-156. An administrator can issue a reset code for every other account,
    so that account is the master key to the box.

    The refusal lives in the service, not only in the UI, so it cannot be
    clicked past or reached by another route.
    """
    user, _ = await user_factory()

    with pytest.raises(accounts.AccountError, match="two-factor"):
        await accounts.grant_admin(session, user=user)
    assert user.is_admin is False

    user.totp_secret = totp.new_secret()
    await accounts.confirm_totp(
        session, user=user, code=totp._code_for_step(user.totp_secret, totp.current_step())
    )
    await accounts.grant_admin(session, user=user)
    await session.commit()

    assert user.is_admin is True


# --------------------------------------------------------------------------
# Suspension
# --------------------------------------------------------------------------


async def test_suspension_ends_sessions_and_keeps_every_byte(
    session, admin, user_factory
) -> None:
    user, _library = await user_factory()
    session.add(
        RefreshToken(
            user_id=user.id, token_hash=uuid.uuid4().hex,
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
    )
    await session.commit()

    await accounts.suspend(session, user=user, by=admin)
    await session.commit()
    await session.refresh(user)

    assert user.is_active is False
    assert user.suspended_by_id == admin.id
    live = (
        await session.execute(
            sa.select(sa.func.count()).select_from(RefreshToken).where(
                RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
            )
        )
    ).scalar_one()
    assert live == 0
    # REQ-090: the library and its documents are untouched.
    still_there = (
        await session.execute(
            sa.select(sa.func.count()).select_from(Membership).where(
                Membership.user_id == user.id
            )
        )
    ).scalar_one()
    assert still_there == 1


async def test_an_admin_cannot_suspend_themselves(session, admin) -> None:
    """A single-administrator system that locks out its administrator has no
    way back in."""
    with pytest.raises(accounts.AccountError, match="your own account"):
        await accounts.suspend(session, user=admin, by=admin)


async def test_restoring_gives_the_account_back(session, admin, user_factory) -> None:
    user, _ = await user_factory()
    await accounts.suspend(session, user=user, by=admin)
    await accounts.restore(session, user=user)
    await session.commit()
    await session.refresh(user)

    assert user.is_active is True
    assert user.suspended_at is None


async def test_a_suspended_account_cannot_log_in(session, client, user_factory) -> None:
    from tests.conftest import PASSWORD

    user, _ = await user_factory()
    user.is_active = False
    await session.commit()

    response = await client.post(
        "/api/auth/login", json={"email": user.email, "password": PASSWORD}
    )
    assert response.status_code == 401


async def test_regenerating_recovery_codes_retires_them_rather_than_deleting(
    session, user_factory
) -> None:
    """REQ-090, and a distinction worth keeping: "which codes did I spend" and
    "which did I regenerate away" look alike and are not the same question."""
    from api.db.models import RecoveryCode

    user, _ = await user_factory()
    first = await accounts.new_recovery_codes(session, user)
    await session.commit()
    await accounts.new_recovery_codes(session, user)
    await session.commit()

    rows = (
        await session.execute(
            sa.select(RecoveryCode).where(RecoveryCode.user_id == user.id)
        )
    ).scalars().all()
    assert len(rows) == accounts.RECOVERY_CODE_COUNT * 2, "nothing was removed"
    assert sum(1 for r in rows if r.superseded_at is not None) == accounts.RECOVERY_CODE_COUNT

    user.totp_secret = totp.new_secret()
    user.totp_confirmed_at = datetime.now(UTC)
    await session.commit()
    with pytest.raises(accounts.AccountError):
        await accounts.check_second_factor(session, user=user, code=first[0])
