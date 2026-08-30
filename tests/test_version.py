"""What is running, and whether the pieces agree (T-11.5 to T-11.7).

"Which code is this?" is the first question of every production problem. The
answers here come from the build rather than from a number in the repository,
because a version somebody has to remember to bump is a version that is wrong.
"""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from api import version
from api.db.models import ServiceHeartbeat


@pytest.fixture(autouse=True)
async def no_heartbeats(session):
    await session.execute(sa.delete(ServiceHeartbeat))
    await session.commit()
    yield


def test_an_unstamped_build_says_unknown_rather_than_guessing(monkeypatch) -> None:
    """A locally-built image has no commit, and inventing one would be worse
    than admitting it."""
    for name in ("BINDERY_COMMIT", "BINDERY_BUILT_AT", "BINDERY_REF"):
        monkeypatch.delenv(name, raising=False)

    build = version.build_of_this_process()
    assert build.commit == "unknown"
    assert build.short == "unknown"


def test_the_short_form_is_a_short_commit(monkeypatch) -> None:
    monkeypatch.setenv("BINDERY_COMMIT", "0123456789abcdef0123456789abcdef01234567")
    assert version.build_of_this_process().short == "0123456"


async def test_a_worker_on_a_different_commit_is_a_mismatch(
    session, monkeypatch
) -> None:
    """The failure this exists for: the worker is a separate image, pulled
    separately, and can be a different commit while every screen renders."""
    monkeypatch.setenv("BINDERY_COMMIT", "a" * 40)
    session.add(
        ServiceHeartbeat(
            service="worker", commit="b" * 40, built_at="then", ref="main",
            last_seen_at=datetime.now(UTC),
        )
    )
    await session.commit()

    report = await version.report(session)
    assert report["mismatch"] is True
    assert report["services"]["worker"]["short"] == "bbbbbbb"
    assert report["services"]["api"]["short"] == "aaaaaaa"


async def test_matching_commits_are_not_a_mismatch(session, monkeypatch) -> None:
    monkeypatch.setenv("BINDERY_COMMIT", "a" * 40)
    session.add(
        ServiceHeartbeat(
            service="worker", commit="a" * 40, built_at="then", ref="main",
            last_seen_at=datetime.now(UTC),
        )
    )
    await session.commit()

    assert (await version.report(session))["mismatch"] is False


async def test_an_unstamped_service_is_not_reported_as_a_mismatch(
    session, monkeypatch
) -> None:
    """`unknown` means a locally-built image, which is normal in development.
    Warning about it permanently would train everyone to ignore the warning."""
    monkeypatch.setenv("BINDERY_COMMIT", "a" * 40)
    session.add(
        ServiceHeartbeat(
            service="worker", commit="unknown", built_at="unknown", ref="unknown",
            last_seen_at=datetime.now(UTC),
        )
    )
    await session.commit()

    assert (await version.report(session))["mismatch"] is False


async def test_a_silent_worker_is_reported_absent_not_current(
    session, monkeypatch
) -> None:
    """Otherwise a worker that died last Tuesday still reports the version it
    was running when it did."""
    monkeypatch.setenv("BINDERY_COMMIT", "a" * 40)
    session.add(
        ServiceHeartbeat(
            service="worker", commit="b" * 40, built_at="then", ref="main",
            last_seen_at=datetime.now(UTC) - timedelta(hours=2),
        )
    )
    await session.commit()

    report = await version.report(session)
    assert report["services"]["worker"]["stale"] is True
    assert report["mismatch"] is False, "a dead worker is a different alarm"


async def test_announcing_twice_updates_rather_than_accumulates(session) -> None:
    """One row per service: the table answers "what is running", not "what has
    ever run"."""
    await version.announce(session, "worker")
    await version.announce(session, "worker")
    await session.commit()

    rows = (
        await session.execute(
            sa.select(sa.func.count()).select_from(ServiceHeartbeat)
        )
    ).scalar_one()
    assert rows == 1


async def test_the_schema_state_compares_applied_against_the_files(session) -> None:
    """Migrations are applied explicitly and never on boot (REQ-114), so "the
    code expects 0019, the database is at 0018" is a real state — and the
    failure it produces is an obscure column error somewhere far away."""
    state = await version.schema_state(session)

    assert state["expected"] is not None, "the head is read from the files"
    assert state["applied"] == state["expected"]
    assert state["in_sync"] is True


def test_the_expected_head_is_read_from_the_migrations_not_hard_coded() -> None:
    """A hard-coded head drifts from the thing it describes, silently, and the
    first sign is a health panel confidently reporting the wrong answer."""
    import inspect

    source = inspect.getsource(version)
    assert "alembic" in source and "versions" in source
    # The current head, whatever it is, must be discovered rather than listed.
    assert '"0019' not in source


def test_two_heads_are_reported_as_unknown_rather_than_picked(tmp_path, monkeypatch) -> None:
    """An un-merged branch is a real condition. Choosing one of the two heads
    would make the panel say something confident and wrong."""
    versions = tmp_path / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "a.py").write_text(
        'revision: str = "aaa"\ndown_revision: str | None = "root"\n'
    )
    (versions / "b.py").write_text(
        'revision: str = "bbb"\ndown_revision: str | None = "root"\n'
    )
    (versions / "root.py").write_text(
        'revision: str = "root"\ndown_revision: str | None = None\n'
    )
    monkeypatch.setattr(
        version, "__file__", str(tmp_path / "api" / "version.py")
    )
    assert version._head_revision() is None


def test_the_revision_parser_does_not_run_past_the_end_of_a_line() -> None:
    """A negated character class matches newlines.

    `(?::[^=]*)?` ran from the docstring's `Revises:` line down to the `=` on
    the real assignment several lines below it, reported the name as "Revises",
    and left every revision parsed as None — which collapsed nineteen
    migrations into a single key and produced a confident, wrong head.
    """
    migration = '''"""A migration.

    Revision ID: 0019_something
    Revises: 0018_earlier
    """

revision: str = "0019_something"
down_revision: str | None = "0018_earlier"
'''
    assert version._assigned(migration, "revision") == "0019_something"
    assert version._assigned(migration, "down_revision") == "0018_earlier"
    assert version._assigned(migration, "Revises") is None
