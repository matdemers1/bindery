"""When a replication run is due, and what happened to the last one
(T-13.6, REQ-163 to REQ-165, ADR-010).

The scheduling is the whole of this file. It is separated from the S3 code
precisely so it can be tested without AWS — "should a run happen now" has
nothing to do with buckets.

Two cadences, deliberately expressed differently, and each for a reason:

*Daily* is an interval — 20 hours rather than 24, so a run does not creep an
hour later each day until it lands in the afternoon, and an interval rather
than a clock time so a machine switched off overnight runs when it comes back
instead of skipping a day and calling it a success.

*Weekly* is a calendar week, because **the object key is the ISO week.** The
bucket can physically hold one weekly generation per week, so that is not a
policy choice. A seven-day interval would drift across week boundaries and
silently leave a week with no generation at all.
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from api import offsite_runs
from api.db.models import OffsiteRun
from api.offsite import Kind

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)  # a Sunday, ISO week 35


@pytest.fixture(autouse=True)
async def no_runs(session):
    await session.execute(sa.delete(OffsiteRun))
    await session.commit()


async def record(session, kind: Kind, *, at: datetime, state=offsite_runs.SUCCEEDED):
    run = OffsiteRun(kind=kind.value, state=state, trigger="schedule", started_at=at)
    session.add(run)
    await session.commit()
    return run


async def test_with_no_history_the_weekly_run_goes_first(session):
    """On a fresh install both are due. Weekly wins because it has a deadline it
    cannot make up later — the ISO week ends whether or not it ran."""
    due = await offsite_runs.what_is_due(session, now=NOW)
    assert due.kind is Kind.WEEKLY


async def test_daily_is_due_once_the_last_one_is_twenty_hours_old(session):
    await record(session, Kind.WEEKLY, at=NOW - timedelta(hours=1))

    await record(session, Kind.DAILY, at=NOW - timedelta(hours=19))
    assert await offsite_runs.what_is_due(session, now=NOW) is None

    await record(session, Kind.DAILY, at=NOW - timedelta(hours=21))
    # The newest daily is still 19h old, so still not due.
    assert await offsite_runs.what_is_due(session, now=NOW) is None


async def test_daily_becomes_due_and_does_not_creep_later_each_day(session):
    """Twenty hours, not twenty-four. At exactly 24 the run drifts an hour later
    every day until it is happening in the middle of the afternoon."""
    await record(session, Kind.WEEKLY, at=NOW - timedelta(hours=1))
    await record(session, Kind.DAILY, at=NOW - timedelta(hours=20, minutes=1))

    due = await offsite_runs.what_is_due(session, now=NOW)
    assert due is not None and due.kind is Kind.DAILY


async def test_a_machine_that_was_off_runs_when_it_comes_back(session):
    """Not "skips the slot and reports success"."""
    await record(session, Kind.WEEKLY, at=NOW - timedelta(hours=2))
    await record(session, Kind.DAILY, at=NOW - timedelta(days=4))

    due = await offsite_runs.what_is_due(session, now=NOW)
    assert due is not None and due.kind is Kind.DAILY


async def test_weekly_is_due_once_per_iso_week_not_every_seven_days(session):
    """The object key *is* the ISO week, so one per week is the only thing the
    naming scheme can express."""
    # Note the trap this test walked into while being written: Sunday 30 August
    # and Monday 24 August are the *same* ISO week, six days apart. "Six days
    # ago" is not "last week", and "seven days ago" can skip a week entirely —
    # which is the whole argument against an interval rule here.
    previous_week = datetime(2026, 8, 20, 3, 0, tzinfo=UTC)  # ISO week 34
    assert previous_week.isocalendar()[1] != NOW.isocalendar()[1]
    await record(session, Kind.WEEKLY, at=previous_week)

    due = await offsite_runs.what_is_due(session, now=NOW)
    assert due is not None and due.kind is Kind.WEEKLY


async def test_weekly_is_due_the_day_after_a_sunday_run(session):
    """The case that separates the two rules, and the reason this test exists.

    A weekly run at Sunday teatime, then Monday morning: **fifteen hours
    apart, and a different ISO week.** A seven-day interval says "not yet" and
    the new week ends with no generation at all — the bucket holds one object
    per ISO week, so a week that is skipped is a week that is simply missing.

    Written after the first version of this test used a ten-day-old run, where
    both rules agree and the interval rule passed the whole suite.
    """
    sunday = datetime(2026, 8, 23, 17, 0, tzinfo=UTC)   # ISO week 34
    monday = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)    # ISO week 35
    assert monday - sunday < timedelta(days=7)
    assert sunday.isocalendar()[1] != monday.isocalendar()[1]

    await record(session, Kind.WEEKLY, at=sunday)
    await record(session, Kind.DAILY, at=monday - timedelta(hours=1))

    due = await offsite_runs.what_is_due(session, now=monday)
    assert due is not None and due.kind is Kind.WEEKLY, (
        "an interval rule would skip this week entirely"
    )


async def test_weekly_does_not_run_twice_in_the_same_week(session):
    await record(session, Kind.WEEKLY, at=NOW - timedelta(hours=6))
    await record(session, Kind.DAILY, at=NOW - timedelta(hours=1))
    assert await offsite_runs.what_is_due(session, now=NOW) is None


async def test_a_failed_run_does_not_count_as_a_success(session):
    """The state that must never read as "recently backed up"."""
    await record(session, Kind.WEEKLY, at=NOW - timedelta(hours=1))
    await record(session, Kind.DAILY, at=NOW - timedelta(minutes=5),
                 state=offsite_runs.FAILED)

    due = await offsite_runs.what_is_due(session, now=NOW)
    assert due is not None and due.kind is Kind.DAILY


async def test_a_human_request_jumps_the_schedule(session):
    """Someone watching the screen after fixing a setting should not wait behind
    a weekly run."""
    await record(session, Kind.WEEKLY, at=NOW - timedelta(hours=1))
    await record(session, Kind.DAILY, at=NOW - timedelta(hours=1))
    who = uuid.uuid4()

    requested = await offsite_runs.request(session, Kind.DAILY, requested_by=who)
    await session.commit()
    assert requested is not None

    due = await offsite_runs.what_is_due(session, now=NOW)
    assert due.trigger == "manual"
    assert due.requested_by == who
    assert due.run_id == requested.id


async def test_a_second_request_while_one_is_pending_is_refused(session):
    """Two presses of Replicate now must not become two uploads. Versioning is
    on and nothing can delete, so a duplicate object version is permanent."""
    assert await offsite_runs.request(session, Kind.DAILY, requested_by=None) is not None
    await session.commit()
    assert await offsite_runs.request(session, Kind.DAILY, requested_by=None) is None


async def test_only_one_run_can_be_in_flight(session):
    """Enforced by the partial unique index, not by the worker being careful."""
    due = await offsite_runs.what_is_due(session, now=NOW)
    first = await offsite_runs.begin(session, due)
    assert first is not None

    second = await offsite_runs.begin(session, due)
    assert second is None, "two workers both claimed a replication run"


async def test_an_abandoned_run_is_released_rather_than_blocking_forever(session):
    """A `running` row held by a dead worker blocks every future run — the
    unique index sees to that — so it reports as "nothing due" rather than as an
    outage. That is the failure this exists to prevent."""
    due = await offsite_runs.what_is_due(session, now=NOW)
    run = await offsite_runs.begin(session, due)
    run.started_at = NOW - timedelta(hours=9)
    await session.commit()

    assert await offsite_runs.what_is_due(session, now=NOW) is None, (
        "a held run should block the schedule while it is held"
    )

    released = await offsite_runs.release_stale(session, older_than=timedelta(hours=6))
    assert released == 1
    assert await offsite_runs.what_is_due(session, now=NOW) is not None


async def test_status_with_no_history_is_stale_not_green(session):
    """No successful run is the most alarming state this can be in, not the most
    neutral. A dashboard that is green because nothing has happened yet is the
    exact failure the Trust screen exists to prevent."""
    state = await offsite_runs.status(session, now=NOW)
    assert state["last_success_at"] is None
    assert state["stale"] is True


async def test_status_goes_stale_after_two_missed_days(session):
    await record(session, Kind.DAILY, at=NOW - timedelta(hours=47))
    assert (await offsite_runs.status(session, now=NOW))["stale"] is False

    await session.execute(sa.delete(OffsiteRun))
    await record(session, Kind.DAILY, at=NOW - timedelta(hours=49))
    assert (await offsite_runs.status(session, now=NOW))["stale"] is True


async def test_a_weekly_success_keeps_the_status_fresh(session):
    """A weekly run copies the same blobs a daily one does, so "is the archive
    safe" depends on the newest success of either kind."""
    await record(session, Kind.WEEKLY, at=NOW - timedelta(hours=2))
    state = await offsite_runs.status(session, now=NOW)
    assert state["stale"] is False
    assert state["last_daily_at"] is None


def test_the_worker_actually_starts_the_replication_loop():
    """A loop nobody starts is inert, and inert is indistinguishable from
    working until someone needs a restore.

    Structural rather than behavioural: `main()` runs forever, so the thing to
    assert is that the task is created at all. The same shape of guard caught a
    fourth unbounded-thinking site during Phase 9.
    """
    import ast

    source = (Path(__file__).resolve().parent.parent / "worker" / "runner.py").read_text()
    tree = ast.parse(source)
    main = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main"
    )
    started = {
        node.func.id
        for node in ast.walk(main)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_offsite_replication" in started, (
        "the replication loop is defined but never started — the feature would "
        "be silently inert"
    )


def test_the_replication_loop_does_not_use_the_job_queue():
    """The trap ADR-010 exists to record.

    A global job has a null idempotency key and `enqueue` is
    `on_conflict_do_nothing`, so the first run would insert and every run after
    it would silently no-op — forever, whether the first succeeded or
    dead-lettered. The queue would look healthy while nothing left the building.

    Checked against the parsed function rather than its text: the docstring says
    "deliberately not a JobStage", and a grep cannot tell prose from use. The
    first version of this test failed on its own explanation.
    """
    import ast

    source = (Path(__file__).resolve().parent.parent / "worker" / "runner.py").read_text()
    loop = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_offsite_replication"
    )
    names = {node.id for node in ast.walk(loop) if isinstance(node, ast.Name)}
    attributes = {
        f"{node.value.id}.{node.attr}"
        for node in ast.walk(loop)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    }
    assert "JobStage" not in names
    assert not {a for a in attributes if a.startswith("queue.")}, (
        "the replication loop reaches into the job queue"
    )


def test_both_images_can_take_a_dump():
    """Replication runs in the worker, and it shells out to pg_dump.

    The api image has carried the client since Phase 6, when backups ran from
    the api and only from the api. The first real replication run failed with
    "pg_dump is not installed in this image" — a clear message, correctly
    recorded as a failed run, and invisible to every test in this suite, which
    executes in the api image where the binary has always been present.

    Pinned to 16 in both: a 17 client dumps a 16 server perfectly and writes an
    archive format pg_restore 16 refuses, which is a failure that appears only
    when you try to restore.
    """
    root = Path(__file__).resolve().parent.parent
    for image in ("Dockerfile.api", "Dockerfile.worker"):
        text = (root / "infra" / image).read_text()
        assert "postgresql-client-16" in text, (
            f"{image} cannot run pg_dump — a backup taken from it would fail at "
            "the moment it is needed"
        )
