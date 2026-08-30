"""When a replication run is due, and what happened to the last one
(T-13.6, REQ-163 to REQ-165, ADR-010).

Scheduling and bookkeeping live here; `api/offsite.py` is the part that talks to
S3. The split is deliberate — the question "should a run happen now" has nothing
to do with AWS and is far easier to test without it.

**Two cadences, deliberately expressed differently.**

*Daily* is an interval: due when the newest success is more than 20 hours old.
Twenty rather than 24 so the run does not creep an hour later each day until it
lands in the middle of the afternoon, and an interval rather than a clock time
so a machine that was switched off overnight simply runs when it comes back
rather than skipping a day and reporting success.

*Weekly* is a calendar week, because **the object key is the ISO week.** The
bucket can hold exactly one weekly generation per week, so "one per ISO week" is
not a policy choice — it is the only thing the naming scheme can express. A
seven-day interval would drift across week boundaries and silently leave a week
with no generation at all.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import OffsiteRun
from api.offsite import Kind

log = logging.getLogger("bindery.offsite.runs")

REQUESTED = "requested"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

DAILY_INTERVAL = timedelta(hours=20)

# How old the newest success may be before it stops being a warning and starts
# being the same alert a stalled pipeline raises (REQ-165). Two missed daily
# runs, not one — a single failure is a bad night, two in a row is a fault.
STALE_AFTER = timedelta(hours=48)


@dataclass(frozen=True)
class Due:
    kind: Kind
    trigger: str
    requested_by: uuid.UUID | None = None
    run_id: uuid.UUID | None = None


async def _newest_success(session: AsyncSession, kind: Kind) -> OffsiteRun | None:
    return (
        await session.execute(
            sa.select(OffsiteRun)
            .where(OffsiteRun.kind == kind.value, OffsiteRun.state == SUCCEEDED)
            .order_by(OffsiteRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def what_is_due(session: AsyncSession, *, now: datetime | None = None) -> Due | None:
    """The next thing to run, or nothing.

    A human request wins over the schedule: someone watching the screen after
    changing a setting should not wait behind a weekly run.
    """
    now = now or datetime.now(UTC)

    # Nothing is due while one is in flight. `begin` would refuse anyway — the
    # partial unique index sees to that — but only by raising IntegrityError on
    # an insert this function had already said was warranted, every pass, with a
    # log line each time. Saying "nothing is due" is both cheaper and true.
    in_flight = (
        await session.execute(
            sa.select(OffsiteRun.id).where(OffsiteRun.state == RUNNING).limit(1)
        )
    ).scalar_one_or_none()
    if in_flight is not None:
        return None

    requested = (
        await session.execute(
            sa.select(OffsiteRun)
            .where(OffsiteRun.state == REQUESTED)
            .order_by(OffsiteRun.started_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if requested is not None:
        return Due(
            kind=Kind(requested.kind), trigger=requested.trigger,
            requested_by=requested.requested_by, run_id=requested.id,
        )

    # Weekly first: if both are due it is the start of a week, and the weekly
    # generation is the one with a deadline it cannot make up later.
    weekly = await _newest_success(session, Kind.WEEKLY)
    if weekly is None or weekly.started_at.isocalendar()[:2] != now.isocalendar()[:2]:
        return Due(kind=Kind.WEEKLY, trigger="schedule")

    daily = await _newest_success(session, Kind.DAILY)
    if daily is None or (now - daily.started_at) > DAILY_INTERVAL:
        return Due(kind=Kind.DAILY, trigger="schedule")
    return None


async def request(
    session: AsyncSession, kind: Kind, *, requested_by: uuid.UUID | None
) -> OffsiteRun | None:
    """Ask for a run. Returns None if one is already queued or in flight.

    The api never uploads. A 300 MB transfer inside a request handler would
    hold a connection open for minutes and die with the request; the worker owns
    the doing, and this is how it is asked.
    """
    existing = (
        await session.execute(
            sa.select(OffsiteRun).where(OffsiteRun.state.in_([REQUESTED, RUNNING]))
        )
    ).scalars().first()
    if existing is not None:
        return None

    run = OffsiteRun(
        kind=kind.value, state=REQUESTED, trigger="manual", requested_by=requested_by
    )
    session.add(run)
    await session.flush()
    return run


async def begin(session: AsyncSession, due: Due) -> OffsiteRun | None:
    """Claim the run. Returns None if another worker already holds one.

    The partial unique index on `state = 'running'` is what makes this safe:
    two workers reaching here at the same instant produce one winner and one
    `IntegrityError`, rather than two concurrent uploads. With versioning on
    and no delete permission, a duplicated object version cannot be cleaned up.
    """
    try:
        if due.run_id is not None:
            run = await session.get(OffsiteRun, due.run_id)
            if run is None or run.state != REQUESTED:
                return None
            run.state = RUNNING
            run.started_at = datetime.now(UTC)
        else:
            run = OffsiteRun(
                kind=due.kind.value, state=RUNNING, trigger=due.trigger,
                requested_by=due.requested_by,
            )
            session.add(run)
        await session.commit()
        return run
    except IntegrityError:
        await session.rollback()
        log.info("another worker is already replicating; standing down")
        return None


async def finish(session: AsyncSession, run: OffsiteRun, result) -> None:
    """Record the outcome. A failure is a row, not a log line nobody reads."""
    run.state = SUCCEEDED if result.ok else FAILED
    run.finished_at = datetime.now(UTC)
    run.detail = result.detail
    run.dump_key = result.dump_key
    run.dump_bytes = result.dump_bytes
    run.blobs_uploaded = result.blobs_uploaded
    run.blobs_skipped = result.blobs_skipped
    run.bytes_sent = result.bytes_sent
    run.failures = result.failures or None
    await session.commit()


async def abandon(session: AsyncSession, run: OffsiteRun, reason: str) -> None:
    """Mark a run failed when it could not even get started.

    Separate from `finish` because there is no result to record — and because a
    `running` row left behind by a crash would hold the partial unique index and
    block every future run. That is the failure this exists to prevent.
    """
    run.state = FAILED
    run.finished_at = datetime.now(UTC)
    run.detail = reason
    await session.commit()


async def release_stale(session: AsyncSession, *, older_than: timedelta) -> int:
    """Free a `running` row whose worker died.

    Nothing else can run while one is held, so an abandoned row is not an
    untidy record — it is an outage that reports itself as "no runs due".
    """
    cutoff = datetime.now(UTC) - older_than
    result = await session.execute(
        sa.update(OffsiteRun)
        .where(OffsiteRun.state == RUNNING, OffsiteRun.started_at < cutoff)
        .values(
            state=FAILED,
            finished_at=datetime.now(UTC),
            detail="the worker running this did not finish — released by the next pass",
        )
    )
    await session.commit()
    return result.rowcount or 0


async def status(session: AsyncSession, *, now: datetime | None = None) -> dict:
    """What the Trust screen shows, and what the health alert reads (REQ-164/165)."""
    now = now or datetime.now(UTC)
    newest = await _newest_success(session, Kind.DAILY)
    weekly = await _newest_success(session, Kind.WEEKLY)

    # The most recent success of *either* kind is what "is the archive safe"
    # actually depends on. A weekly run copies the same blobs as a daily one.
    latest = max(
        [run for run in (newest, weekly) if run is not None],
        key=lambda run: run.started_at,
        default=None,
    )
    age = (now - latest.started_at) if latest else None

    in_flight = (
        await session.execute(
            sa.select(OffsiteRun).where(OffsiteRun.state.in_([REQUESTED, RUNNING]))
        )
    ).scalars().first()

    return {
        "last_success_at": latest.started_at.isoformat() if latest else None,
        "last_success_age_seconds": int(age.total_seconds()) if age else None,
        # Never "green because nothing has failed yet". No successful run is the
        # most alarming state this can be in, not the most neutral.
        "stale": age is None or age > STALE_AFTER,
        "in_flight": in_flight.state if in_flight else None,
        "last_daily_at": newest.started_at.isoformat() if newest else None,
        "last_weekly_at": weekly.started_at.isoformat() if weekly else None,
    }


async def recent(session: AsyncSession, limit: int = 20) -> list[OffsiteRun]:
    return list(
        (
            await session.execute(
                sa.select(OffsiteRun).order_by(OffsiteRun.started_at.desc()).limit(limit)
            )
        ).scalars().all()
    )
