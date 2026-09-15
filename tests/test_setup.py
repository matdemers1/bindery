"""Phase 19 — claiming a fresh install in the browser (T-19.4 to T-19.6, T-19.10).

REQ-200: a fresh install is claimed with a setup code only the host's operator
can read. REQ-201: the claimed account becomes administrator only after
enrolling TOTP, and an abandoned setup resumes at that step.

**Every test here runs against an empty archive of its own.** The shared
`bindery_test` database holds accounts from every other test file, and "only
while `app_user` has zero rows" cannot be exercised against a table that is
never empty. So the module migrates a template database once and each test gets
a fresh copy of it (`CREATE DATABASE … TEMPLATE`, a file copy), with the api's
session dependency pointed at that copy. The race test depends on this being a
real database with real concurrent connections: the property under test is a
Postgres lock, and nothing short of Postgres can falsify it.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api import cli, eventlog, first_run
from api.auth import throttle, totp
from api.auth.passwords import hash_password
from api.config import get_settings
from api.db.enums import LibraryKind, MembershipRole
from api.db.models import AppUser, AuditEvent, EventLog, Library, Membership, Setting
from api.db.session import SessionFactory, get_session
from api.main import app

REPO = Path(__file__).resolve().parent.parent
GOOD_PASSWORD = "ledger obelisk hangar 41"


# ---------------------------------------------------------------------------
# An empty archive per test
# ---------------------------------------------------------------------------


def _url(database: str | None = None) -> str:
    url = sa.engine.make_url(get_settings().database_url)
    if database is not None:
        url = url.set(database=database)
    return url.render_as_string(hide_password=False)


def _names() -> tuple[str, str, str]:
    base = sa.engine.make_url(get_settings().database_url).database
    return f"{base}_setup_template", f"{base}_setup", f"{base}_setup_bare"


async def _admin(*statements: str) -> None:
    engine = create_async_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            for statement in statements:
                await connection.execute(sa.text(statement))
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
async def template() -> str:
    name, copy, bare = _names()
    await _admin(
        f'DROP DATABASE IF EXISTS "{copy}" WITH (FORCE)',
        f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)',
        f'CREATE DATABASE "{name}"',
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=REPO,
        env={**os.environ, "DATABASE_URL": _url(name)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    yield name
    await _admin(
        f'DROP DATABASE IF EXISTS "{copy}" WITH (FORCE)',
        f'DROP DATABASE IF EXISTS "{bare}" WITH (FORCE)',
        f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)',
    )


@pytest.fixture
async def archive(template, monkeypatch):
    """A session factory for a freshly migrated, completely empty archive."""
    _, copy, _ = _names()
    await _admin(
        f'DROP DATABASE IF EXISTS "{copy}" WITH (FORCE)',
        f'CREATE DATABASE "{copy}" TEMPLATE "{template}"',
    )
    engine = create_async_engine(_url(copy))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def scratch_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = scratch_session
    monkeypatch.setattr(cli, "SessionFactory", factory)
    try:
        yield factory
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver")


async def _issue(factory) -> str:
    async with factory() as session:
        code = await first_run.ensure_setup_code(session)
    assert code is not None
    return code


def _body(code: str, **overrides) -> dict:
    return {
        "code": code,
        "email": "owner@example.test",
        "password": GOOD_PASSWORD,
        "display_name": "Owner",
        "library_name": "Household",
        **overrides,
    }


def _from(ip: str) -> dict[str, str]:
    return {"cf-connecting-ip": ip}


async def _setting(factory, key: str) -> str | None:
    async with factory() as session:
        return (
            await session.execute(sa.select(Setting.value).where(Setting.key == key))
        ).scalar_one_or_none()


async def _count(factory, model) -> int:
    async with factory() as session:
        return (await session.execute(sa.select(sa.func.count()).select_from(model))).scalar_one()


async def _state(client: AsyncClient) -> str:
    response = await client.get("/api/setup")
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"state"}, "the one fact, and nothing else"
    return response.json()["state"]


async def _enrol_totp(client: AsyncClient) -> None:
    started = await client.post("/api/account/totp/start")
    assert started.status_code == 200, started.text
    confirmed = await client.post(
        "/api/account/totp/confirm",
        json={"code": totp._code_for_step(started.json()["secret"], totp.current_step())},
    )
    assert confirmed.status_code == 200, confirmed.text


# ---------------------------------------------------------------------------
# The code
# ---------------------------------------------------------------------------


def test_the_code_is_twelve_unambiguous_characters_in_groups_of_four() -> None:
    for _ in range(200):
        code = first_run.generate_code()
        assert len(code) == 12
        assert not set(code) & set("0O1IL")
    shown = first_run.display("ABCDEFGHJKMN")
    assert shown == "ABCD-EFGH-JKMN"
    assert first_run.normalise(" abcd-efgh jkmn\n") == "ABCDEFGHJKMN"
    assert first_run.normalise("ABCD-EFGH-JKM0") is None, "0 is not in the alphabet"
    assert first_run.normalise("ABCD-EFGH") is None


async def test_ensure_setup_code_prints_it_and_persists_no_plaintext(
    archive, capsys, caplog
) -> None:
    """stdout, and only stdout. `logging` would put it in event_log (eventlog.py)."""
    # Whatever other tests left queued belongs to the shared database, not here.
    eventlog.install()
    while await eventlog.drain_once(SessionFactory):
        pass

    caplog.set_level("DEBUG")
    code = await _issue(archive)
    printed = capsys.readouterr().out

    assert first_run.display(code) in printed
    assert all(line.startswith("[bindery setup]") for line in printed.splitlines()), (
        "every line of the banner greps as one block"
    )
    assert "setup-code" in printed, "it says how to print another"

    for variant in (code, first_run.display(code), code.lower()):
        assert variant not in caplog.text, "the code went through logging"

    while await eventlog.drain_once(archive):
        pass
    async with archive() as session:
        settings = (await session.execute(sa.select(Setting.key, Setting.value))).all()
        messages = (await session.execute(sa.select(EventLog.message, EventLog.detail))).all()
        audits = (await session.execute(sa.select(AuditEvent))).scalars().all()
    stored = " ".join(f"{key} {value}" for key, value in settings)
    logged = " ".join(f"{message} {detail}" for message, detail in messages)
    written = " ".join(f"{a.action} {a.before} {a.after}" for a in audits)
    for variant in (code, first_run.display(code)):
        assert variant not in stored, "plaintext in setting"
        assert variant not in logged, "plaintext in event_log"
        assert variant not in written, "plaintext in audit_event"
    assert await _setting(archive, first_run.CODE_HASH), "only a hash is kept"
    assert "setup_code_issued" in written, "issuing a code is an audited mutation"


async def test_every_boot_mints_a_fresh_code_and_only_the_newest_works(
    archive, client
) -> None:
    first = await _issue(archive)
    second = await _issue(archive)
    assert first != second

    stale = await client.post("/api/setup/claim", json=_body(first), headers=_from("192.0.2.1"))
    assert stale.status_code == 400
    fresh = await client.post("/api/setup/claim", json=_body(second), headers=_from("192.0.2.1"))
    assert fresh.status_code == 200, fresh.text


async def test_booting_an_unmigrated_database_neither_raises_nor_prints(
    template, monkeypatch, capsys
) -> None:
    """A fresh install boots before `make migrate`. The api must still come up."""
    _, _, bare = _names()
    await _admin(f'DROP DATABASE IF EXISTS "{bare}" WITH (FORCE)', f'CREATE DATABASE "{bare}"')
    engine = create_async_engine(_url(bare))
    monkeypatch.setattr(first_run, "BOOT_RETRY_SECONDS", 0.05)
    stopping = asyncio.Event()
    try:
        task = asyncio.create_task(
            first_run.announce_on_boot(stopping, async_sessionmaker(engine))
        )
        await asyncio.sleep(0.3)
        assert not task.done(), "it keeps asking until the tables exist"
        stopping.set()
        await asyncio.wait_for(task, timeout=5)
    finally:
        await engine.dispose()
    assert "[bindery setup]" not in capsys.readouterr().out


async def test_leftover_code_settings_are_cleared_once_accounts_exist(archive) -> None:
    await _issue(archive)
    async with archive() as session:
        session.add(AppUser(email="someone@example.test", password_hash=hash_password("x" * 20)))
        await session.commit()
        assert await first_run.ensure_setup_code(session) is None

    assert await _setting(archive, first_run.CODE_HASH) is None
    assert await _setting(archive, first_run.CODE_CREATED_AT) is None


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "formatting",
    [
        lambda code: code,
        first_run.display,
        lambda code: first_run.display(code).lower(),
        lambda code: "  " + " ".join(first_run.display(code).split("-")) + "\n",
    ],
    ids=["bare", "grouped", "lowercase", "spaced"],
)
async def test_the_printed_code_claims_the_archive(archive, client, formatting) -> None:
    code = await _issue(archive)

    response = await client.post(
        "/api/setup/claim", json=_body(formatting(code)), headers=_from("192.0.2.10")
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"id", "email", "display_name", "is_admin"}, "UserOut, exactly"
    assert body["email"] == "owner@example.test"
    assert body["display_name"] == "Owner"
    assert body["is_admin"] is False, "administrator only after two-factor (REQ-201)"

    # Signed in exactly like login: the session cookie works.
    me = await client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["id"] == body["id"]

    async with archive() as session:
        user = await session.get(AppUser, uuid.UUID(body["id"]))
        assert user is not None
        membership = (
            await session.execute(sa.select(Membership).where(Membership.user_id == user.id))
        ).scalar_one()
        library = await session.get(Library, membership.library_id)
        audit = (
            await session.execute(
                sa.select(AuditEvent).where(
                    AuditEvent.entity_id == user.id, AuditEvent.action == "account_created"
                )
            )
        ).scalar_one()
    assert membership.role == MembershipRole.OWNER
    assert library is not None and library.name == "Household"
    assert library.kind == LibraryKind.PERSONAL
    assert audit.after is not None and audit.after["via"] == "setup"
    assert audit.actor_id == user.id

    assert await _setting(archive, first_run.OWNER_USER_ID) == body["id"]


async def test_the_code_hash_is_cleared_at_claim(archive, client) -> None:
    """Cleared, not merely left unusable because accounts now exist."""
    code = await _issue(archive)
    response = await client.post("/api/setup/claim", json=_body(code), headers=_from("192.0.2.11"))
    assert response.status_code == 200, response.text

    assert await _setting(archive, first_run.CODE_HASH) is None
    assert await _setting(archive, first_run.CODE_CREATED_AT) is None


async def test_a_second_claim_is_409(archive, client) -> None:
    code = await _issue(archive)
    first = await client.post("/api/setup/claim", json=_body(code), headers=_from("192.0.2.12"))
    assert first.status_code == 200, first.text

    async with _client() as stranger:
        for attempt in (_body(code), _body(code, email="other@example.test"), _body("WRONG")):
            again = await stranger.post(
                "/api/setup/claim", json=attempt, headers=_from("192.0.2.13")
            )
            assert again.status_code == 409, again.text
    assert await _count(archive, AppUser) == 1


async def test_every_bad_code_gets_the_same_answer(archive, client, monkeypatch) -> None:
    messages = set()

    # No code issued at all.
    missing = await client.post(
        "/api/setup/claim", json=_body("ABCD-EFGH-JKMN"), headers=_from("192.0.2.20")
    )
    assert missing.status_code == 400
    messages.add(missing.json()["detail"])

    code = await _issue(archive)
    wrong = "".join("A" if ch != "A" else "B" for ch in code)
    for sent, ip in [(wrong, "192.0.2.21"), ("", "192.0.2.22"), ("not-a-code!", "192.0.2.23")]:
        response = await client.post("/api/setup/claim", json=_body(sent), headers=_from(ip))
        assert response.status_code == 400, response.text
        messages.add(response.json()["detail"])

    # Expired: the right code, too late.
    async with archive() as session:
        await session.execute(
            sa.update(Setting)
            .where(Setting.key == first_run.CODE_CREATED_AT)
            .values(value=(datetime.now(UTC) - first_run.CODE_TTL - timedelta(seconds=1))
                    .isoformat())
        )
        await session.commit()
    expired = await client.post("/api/setup/claim", json=_body(code), headers=_from("192.0.2.24"))
    assert expired.status_code == 400, expired.text
    messages.add(expired.json()["detail"])

    assert messages == {"that setup code is not valid"}
    assert await _count(archive, AppUser) == 0, "nothing was created by a bad code"


async def test_claims_are_throttled_by_address(archive, client) -> None:
    code = await _issue(archive)
    ip = "198.51.100.77"
    for _ in range(throttle.IP_FREE_ATTEMPTS + 1):
        response = await client.post(
            "/api/setup/claim", json=_body("ABCD-EFGH-JKMN"), headers=_from(ip)
        )
        assert response.status_code == 400, response.text

    limited = await client.post("/api/setup/claim", json=_body(code), headers=_from(ip))
    assert limited.status_code == 429, limited.text
    assert "Retry-After" in limited.headers
    assert await _count(archive, AppUser) == 0, "even the right code waits out the throttle"


async def test_a_weak_password_is_422_and_spends_nothing(archive, client) -> None:
    code = await _issue(archive)
    response = await client.post(
        "/api/setup/claim", json=_body(code, password="short"), headers=_from("192.0.2.30")
    )
    assert response.status_code == 422, response.text
    assert "characters" in response.json()["detail"], "the validator's own message"

    assert await _count(archive, AppUser) == 0
    retry = await client.post("/api/setup/claim", json=_body(code), headers=_from("192.0.2.30"))
    assert retry.status_code == 200, "the code is still good after a refused password"


async def test_racing_claims_produce_exactly_one_owner(archive) -> None:
    """Checked in the same transaction as the insert, under a lock (REQ-200).

    Different addresses, so the throttle's per-address lock cannot be what
    serialises them — only the first-run lock can.
    """
    code = await _issue(archive)
    racers = 8

    async def attempt(index: int):
        async with _client() as racer:
            return await racer.post(
                "/api/setup/claim",
                json=_body(code, email=f"racer-{index}@example.test"),
                headers=_from(f"203.0.113.{index + 1}"),
            )

    responses = await asyncio.gather(*(attempt(i) for i in range(racers)))
    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(200) == 1, statuses
    assert set(statuses) <= {200, 409, 400}, statuses
    assert await _count(archive, AppUser) == 1
    assert await _count(archive, Membership) == 1
    assert await _count(archive, Library) == 1


# ---------------------------------------------------------------------------
# State and completion
# ---------------------------------------------------------------------------


async def test_the_state_moves_through_all_three_and_resumes_after_sign_out(
    archive, client
) -> None:
    assert await _state(client) == "unclaimed"

    code = await _issue(archive)
    claimed = await client.post("/api/setup/claim", json=_body(code), headers=_from("192.0.2.40"))
    assert claimed.status_code == 200, claimed.text
    assert await _state(client) == "needs_second_factor"

    # Abandoned: sign out, sign back in. Still waiting on the second factor.
    assert (await client.post("/api/auth/logout")).status_code == 204
    async with _client() as anonymous:
        assert await _state(anonymous) == "needs_second_factor"
    login = await client.post(
        "/api/auth/login",
        json={"email": "owner@example.test", "password": GOOD_PASSWORD},
        headers=_from("192.0.2.40"),
    )
    assert login.status_code == 200, login.text
    assert await _state(client) == "needs_second_factor"

    await _enrol_totp(client)
    assert await _state(client) == "needs_second_factor", "enrolment alone grants nothing"

    done = await client.post("/api/setup/complete")
    assert done.status_code == 200, done.text
    assert done.json()["is_admin"] is True
    assert await _state(client) == "complete"
    assert await _setting(archive, first_run.OWNER_USER_ID) is None

    async with archive() as session:
        audit = (
            await session.execute(
                sa.select(AuditEvent).where(AuditEvent.action == "admin_granted")
            )
        ).scalar_one()
    assert str(audit.actor_id) == claimed.json()["id"]
    assert audit.after == {"via": "setup"}


async def test_an_instance_with_an_administrator_and_no_owner_is_complete(
    archive, client
) -> None:
    """The live instance: migration 0016 promoted its admin, and no setup owner
    was ever recorded."""
    async with archive() as session:
        session.add(
            AppUser(email="admin@example.test", password_hash=hash_password("x" * 20),
                    is_admin=True)
        )
        await session.commit()
    assert await _state(client) == "complete"


async def test_complete_is_refused_without_two_factor(archive, client) -> None:
    code = await _issue(archive)
    claimed = await client.post("/api/setup/claim", json=_body(code), headers=_from("192.0.2.50"))
    assert claimed.status_code == 200, claimed.text

    refused = await client.post("/api/setup/complete")
    assert refused.status_code == 409, refused.text
    assert "two-factor" in refused.json()["detail"]
    async with archive() as session:
        user = await session.get(AppUser, uuid.UUID(claimed.json()["id"]))
    assert user is not None and user.is_admin is False
    assert await _state(client) == "needs_second_factor"


async def test_complete_is_refused_for_anyone_but_the_owner(archive, client) -> None:
    code = await _issue(archive)
    claimed = await client.post("/api/setup/claim", json=_body(code), headers=_from("192.0.2.60"))
    assert claimed.status_code == 200, claimed.text

    # A second account — with two-factor, so the only thing wrong is who it is.
    async with archive() as session:
        session.add(AppUser(email="guest@example.test", password_hash=hash_password(GOOD_PASSWORD)))
        await session.commit()
    async with _client() as guest:
        login = await guest.post(
            "/api/auth/login",
            json={"email": "guest@example.test", "password": GOOD_PASSWORD},
            headers=_from("192.0.2.61"),
        )
        assert login.status_code == 200, login.text
        await _enrol_totp(guest)

        refused = await guest.post("/api/setup/complete")
        assert refused.status_code == 403, refused.text
        assert "claimed" in refused.json()["detail"]

    async with archive() as session:
        admins = (
            await session.execute(sa.select(sa.func.count()).where(AppUser.is_admin.is_(True)))
        ).scalar_one()
    assert admins == 0


async def test_complete_is_refused_once_an_administrator_exists(archive, client) -> None:
    code = await _issue(archive)
    claimed = await client.post("/api/setup/claim", json=_body(code), headers=_from("192.0.2.70"))
    assert claimed.status_code == 200, claimed.text
    await _enrol_totp(client)

    # Somebody else became administrator some other way first.
    async with archive() as session:
        session.add(
            AppUser(email="other-admin@example.test", password_hash=hash_password("x" * 20),
                    is_admin=True)
        )
        await session.commit()

    refused = await client.post("/api/setup/complete")
    assert refused.status_code == 409, refused.text
    assert "already complete" in refused.json()["detail"]
    async with archive() as session:
        user = await session.get(AppUser, uuid.UUID(claimed.json()["id"]))
    assert user is not None and user.is_admin is False


async def test_complete_requires_a_session(archive) -> None:
    async with _client() as anonymous:
        assert (await anonymous.post("/api/setup/complete")).status_code == 401


# ---------------------------------------------------------------------------
# The CLI path
# ---------------------------------------------------------------------------


async def test_create_user_on_an_empty_archive_records_the_setup_owner(
    archive, client, capsys
) -> None:
    await _issue(archive)
    await cli._create_user("First@Example.test", GOOD_PASSWORD, "Household", "personal")

    async with archive() as session:
        first = (
            await session.execute(sa.select(AppUser).where(AppUser.email == "first@example.test"))
        ).scalar_one()
    assert first.is_admin is False, "never admin without two-factor"
    assert await _setting(archive, first_run.OWNER_USER_ID) == str(first.id)
    assert await _setting(archive, first_run.CODE_HASH) is None, "the archive is claimed now"
    assert await _state(client) == "needs_second_factor"
    assert "two-factor" in capsys.readouterr().out

    # A second account is just an account.
    await cli._create_user("second@example.test", GOOD_PASSWORD, "Theirs", "personal")
    assert await _setting(archive, first_run.OWNER_USER_ID) == str(first.id)

    # And the CLI-made owner finishes setup the same way a browser claim does.
    login = await client.post(
        "/api/auth/login",
        json={"email": "first@example.test", "password": GOOD_PASSWORD},
        headers=_from("192.0.2.80"),
    )
    assert login.status_code == 200, login.text
    await _enrol_totp(client)
    done = await client.post("/api/setup/complete")
    assert done.status_code == 200, done.text
    assert done.json()["is_admin"] is True


async def test_the_setup_code_command(archive, capsys) -> None:
    assert await cli._setup_code() == 0
    printed = capsys.readouterr().out
    assert "[bindery setup]" in printed
    assert await _setting(archive, first_run.CODE_HASH)

    await cli._create_user("owner@example.test", GOOD_PASSWORD, "Household", "personal")
    capsys.readouterr()
    assert await cli._setup_code() == 1, "non-zero once the archive is claimed"
    captured = capsys.readouterr()
    assert "setup is complete" in captured.err
    assert "[bindery setup]" not in captured.out
