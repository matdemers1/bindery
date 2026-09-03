"""What the archive is doing, and whether anything is quietly broken (T-8.2, REQ-109).

> Silent failure is the worst case for a document archive.

You would not discover that a document never made it in until you went looking
for it and it was not there — possibly years later, and possibly on the day it
mattered. Everything in this module exists to make that impossible.

It reports four things, and the distinction between them is the useful part:

- **Queue depth** — work waiting. Normal, and only interesting as a trend.
- **Failures** — jobs that gave up. Actionable now.
- **Stalls** — a job that claimed a slot and never let go, or a queue that has
  work but nothing running. This is the dangerous one: nothing is *failing*, so
  nothing complains, and the pipeline is simply stopped.
- **Spend** — what the Claude API has cost, because a runaway loop is a bug that
  presents as a bill.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api import models, queue
from api.db import repository
from api.db.enums import JobState, SourceFileState
from api.db.models import Classification, Document, Job, SourceFile

log = logging.getLogger("bindery.health")

# How far past its own lease a job may run before it is called gone rather than
# slow. The threshold used to be a flat thirty minutes, decided here — while
# `queue.STAGE_LEASES` allowed a normalize job forty-five, on the grounds that
# OCR on a large bundled scan legitimately takes that long. The two numbers
# disagreed, and the panel's side of the disagreement raises a *critical* alert,
# which `Notifier.dispatch` turns into a push. A 35-minute OCR pass on exactly
# the workload this archive was built for paged the operator for working
# correctly, and an alert channel that cries wolf during ordinary large ingests
# is one nobody reads.
#
# So the queue's lease table is the single definition, and this is the margin on
# top of it: past its lease the job is late, past its lease *plus this* the
# worker holding it is presumed dead. `reclaim_stale` is what would have put a
# merely-late job back on the queue, so anything still holding a lock this long
# after its lease is not being reclaimed either.
STALE_LOCK_MARGIN = timedelta(minutes=15)

# The worst case across every stage — what "held a lock too long" means for the
# slowest thing the pipeline does. Per-stage thresholds are derived below; this
# is the one number worth quoting in a message or a test.
STALE_LOCK = max(queue.STAGE_LEASES.values()) + STALE_LOCK_MARGIN

# Work queued this long with nothing running means the pipeline has stopped,
# even though nothing has failed.
STALL_AFTER = timedelta(minutes=15)

# Prices live in api/models.py, per model, because the estimate is meaningless
# otherwise: Haiku and Opus differ by roughly 5x, so one hardcoded rate turns a
# runaway-loop tripwire into a random number.


@dataclass
class Alert:
    """Something a person needs to do something about."""

    severity: str  # "warning" | "critical"
    code: str
    message: str
    detail: dict = field(default_factory=dict)


@dataclass
class HealthPanel:
    checked_at: datetime
    queue_depth: dict[str, int]
    running: int
    failed_24h: int
    dead_letter: int
    declined: int
    stuck_jobs: list[dict]
    oldest_queued_seconds: float | None
    stalled: bool
    files_by_state: dict[str, int]
    spend_30d_usd: float
    spend_by_day: list[dict]
    alerts: list[Alert]

    @property
    def healthy(self) -> bool:
        return not any(alert.severity == "critical" for alert in self.alerts)

    def as_dict(self) -> dict:
        return {
            "checked_at": self.checked_at.isoformat(),
            "healthy": self.healthy,
            "queue_depth": self.queue_depth,
            "running": self.running,
            "failed_24h": self.failed_24h,
            "dead_letter": self.dead_letter,
            "declined": self.declined,
            "stuck_jobs": self.stuck_jobs,
            "oldest_queued_seconds": self.oldest_queued_seconds,
            "stalled": self.stalled,
            "files_by_state": self.files_by_state,
            "spend_30d_usd": round(self.spend_30d_usd, 2),
            "spend_by_day": self.spend_by_day,
            "alerts": [
                {
                    "severity": alert.severity,
                    "code": alert.code,
                    "message": alert.message,
                    "detail": alert.detail,
                }
                for alert in self.alerts
            ],
        }


def estimate_cost(usage: dict, model: str | None = None) -> float:
    """Dollars from one classification's token usage, at that model's rates.

    Cache reads are an order of magnitude cheaper than fresh input, so counting
    them as input would make a healthy cache look like a spending problem.
    """
    if not usage:
        return 0.0
    prices = models.pricing(model)
    tokens = {
        "input": usage.get("input_tokens", 0),
        "output": usage.get("output_tokens", 0),
        "cache_write": usage.get("cache_creation_input_tokens", 0),
        "cache_read": usage.get("cache_read_input_tokens", 0),
    }
    return sum(count / 1_000_000 * prices.price(kind) for kind, count in tokens.items())


def _held_past_its_lease(now: datetime) -> sa.ColumnElement[bool]:
    """A running job whose lock has outlived the lease that governs its stage.

    Written once and used by both `collect` and `badge_healthy`, because they
    have to reach the same verdict — `tests/test_health_badge.py` fails the
    build if they drift.
    """
    cutoff = sa.case(
        {
            stage: now - (lease + STALE_LOCK_MARGIN)
            for stage, lease in queue.STAGE_LEASES.items()
        },
        value=Job.stage,
        else_=now - (queue.DEFAULT_LEASE + STALE_LOCK_MARGIN),
    )
    return sa.and_(
        Job.state == JobState.RUNNING,
        Job.locked_at.is_not(None),
        Job.locked_at < cutoff,
    )


# A job that failed and will try again. `queue.fail` puts it back to `QUEUED`
# with its error attached and only *returns* `FAILED`, so almost nothing ever
# carries that state — which is why counting `state == FAILED` alone reported
# "nothing failed" while two documents retried OCR on a loop all day.
# `api/routers/pipeline.py` had already learned this; the panel had not, so the
# Trust screen's "failed today" was structurally zero and the `recent_failures`
# alert could never fire (invariant 8).
_RETRYING = sa.and_(
    Job.state == JobState.QUEUED,
    Job.attempts > 0,
    Job.last_error.is_not(None),
)


async def _offsite_alerts(session: AsyncSession, now: datetime) -> list[Alert]:
    """Whether a copy has left the building lately (T-13.8, REQ-165, REQ-110).

    The Trust screen already shows this, and a screen only helps someone who
    opens it. Replication that quietly stopped in March and is noticed in
    November is the failure this exists to catch — so it raises the same alert a
    stalled pipeline does, on the same panel, through the same notifier.

    Severity is doing real work here, because `Notifier.dispatch` sends only
    critical alerts. A warning is visible to anyone looking at the Health screen
    and pages nobody, which is exactly right for the two states that are either
    a setup step or resolve on their own within minutes.
    """
    from api import offsite, offsite_runs

    config = await offsite.config_from_settings(session)
    if not config.complete:
        # Deliberately a warning. Every fresh install and every dev stack is in
        # this state, and a critical alert here would page on first boot and
        # teach people that this channel is noise. It still appears on the
        # panel, because "there is no offsite copy" is R-09 and staying silent
        # about it is how that risk went two days looking closed.
        return [
            Alert(
                "warning", "offsite_unconfigured",
                "No offsite copy is configured. Both backups are in one building.",
            )
        ]

    state = await offsite_runs.status(session, now=now)
    if state["last_success_at"] is None:
        if state["runs_total"] == 0:
            # Configured seconds ago; the worker checks every ten minutes and a
            # weekly run is due immediately on a fresh archive. Critical here
            # would be a false alarm with a ten-minute lifespan.
            return [
                Alert(
                    "warning", "offsite_pending",
                    "Configured, but no copy has been made yet. The first run "
                    "starts within ten minutes.",
                )
            ]
        return [
            Alert(
                "critical", "offsite_never",
                f"No copy has ever reached the bucket. {state['runs_total']} "
                "attempt(s), all failed.",
                {"attempts": state["runs_total"]},
            )
        ]

    if state["stale"]:
        hours = (state["last_success_age_seconds"] or 0) // 3600
        return [
            Alert(
                "critical", "offsite_stale",
                f"The last offsite copy succeeded {hours} hours ago. "
                "Replication has stopped.",
                {
                    "age_seconds": state["last_success_age_seconds"],
                    "failures_since": state["failures_since_success"],
                },
            )
        ]

    if state["failures_since_success"]:
        # Failing *now*, but not stale yet — the last success is still inside
        # the 48-hour window. This is the early warning: it says the archive is
        # on its way to critical rather than waiting until it arrives.
        #
        # Note the direction. This counts failures *after* the newest success,
        # not before it. A run that failed and then recovered needs no alert;
        # one that succeeded and has failed twice since is the one worth
        # catching a day and a half early.
        return [
            Alert(
                "warning", "offsite_failing",
                f"{state['failures_since_success']} replication run(s) have "
                "failed since the last success.",
                {"count": state["failures_since_success"]},
            )
        ]
    return []


async def collect(
    session: AsyncSession, library_ids: list[uuid.UUID] | None = None
) -> HealthPanel:
    """Queue depth, failures, stalls and spend.

    `library_ids` narrows every count to what the caller can actually reach.
    **It used to be accepted and then ignored** — every query here was global —
    so `/api/health/panel` called the permission helper, threw the answer away,
    and reported the whole archive to everyone. Not a content leak (these are
    counts, job ids and worker ids, and the leak suite covers the route), but it
    meant the sidebar badge could be lit by work in a library the viewer cannot
    open, which is a warning nobody can answer.

    `None` still means the whole archive, and that is the *system* watching
    itself: the worker's health monitor and the webhook notifier pass nothing,
    so a stalled pipeline in someone else's library is still noticed by the
    thing whose job it is to notice.
    """
    now = datetime.now(UTC)
    alerts: list[Alert] = []

    # `sa.true()` rather than a branch at each of the eight call sites below.
    # Every count is filtered the same way or the next one added quietly is not.
    scoped = library_ids is not None
    in_scope = (
        Job.id.in_(repository.visible_job_ids(library_ids)) if scoped else sa.true()
    )
    files_in_scope = (
        SourceFile.library_id.in_(library_ids) if scoped else sa.true()
    )

    state_rows = (
        await session.execute(
            sa.select(Job.state, Job.stage, sa.func.count())
            .where(in_scope)
            .group_by(Job.state, Job.stage)
        )
    ).all()
    queue_depth: dict[str, int] = {}
    running = 0
    for state, stage, count in state_rows:
        if state == JobState.QUEUED:
            queue_depth[str(stage)] = queue_depth.get(str(stage), 0) + count
        elif state == JobState.RUNNING:
            running += count

    failed_24h = (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(Job)
            .where(
                sa.or_(Job.state == JobState.FAILED, _RETRYING),
                Job.updated_at >= now - timedelta(days=1),
                in_scope,
            )
        )
    ) or 0
    # Unacknowledged only. Acknowledging deletes nothing and hides nothing —
    # the job stays on the Pipeline screen with its error — it just stops
    # counting toward "things still demanding attention" (ADR-011, REQ-169).
    dead_letter = (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(Job)
            .where(
                Job.state == JobState.DEAD_LETTER,
                Job.acknowledged_at.is_(None),
                in_scope,
            )
        )
    ) or 0
    # Counted and reported, never alerted on. A declined input is the pipeline
    # having reached a correct answer about something that is not a document
    # (REQ-168).
    declined = (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(Job)
            .where(Job.state == JobState.DECLINED, in_scope)
        )
    ) or 0

    # A worker that died mid-job leaves the lock behind. Nothing fails; the
    # document simply never finishes.
    stuck = (
        (
            await session.execute(
                sa.select(Job)
                .where(_held_past_its_lease(now), in_scope)
                .order_by(Job.locked_at)
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    stuck_jobs = [
        {
            "id": str(job.id),
            "stage": str(job.stage),
            "locked_by": job.locked_by,
            "locked_for_seconds": (now - job.locked_at).total_seconds() if job.locked_at else None,
            "attempts": job.attempts,
        }
        for job in stuck
    ]

    oldest_queued = await session.scalar(
        sa.select(sa.func.min(Job.scheduled_for)).where(
            Job.state == JobState.QUEUED, in_scope
        )
    )
    oldest_seconds = (now - oldest_queued).total_seconds() if oldest_queued else None

    # Work waiting and nothing running is the failure that never announces
    # itself: the pipeline has stopped, but nothing is in a failed state.
    stalled = bool(
        oldest_seconds
        and oldest_seconds > STALL_AFTER.total_seconds()
        and running == 0
    )

    file_rows = (
        await session.execute(
            sa.select(SourceFile.state, sa.func.count())
            .where(files_in_scope)
            .group_by(SourceFile.state)
        )
    ).all()
    files_by_state = {str(state): count for state, count in file_rows}

    # Summed in Postgres, by day and by model, rather than row by row in
    # Python. This used to transfer every classification of the last thirty
    # days — the `usage` JSONB included — and add them up here, which coupled
    # the cost of the health read to how much classification had just happened.
    # A backlog import is precisely when there are twenty thousand of those rows
    # *and* when the panel is refreshed most often. What comes back now is at
    # most thirty days x the handful of models in `api/models.py`, because the
    # token counts are what aggregate and the per-model rates are what cannot.
    # `AT TIME ZONE 'UTC'` before the cast, so the grouping is the same day
    # boundary `created_at.date()` gave in Python and not whatever the server's
    # TimeZone setting happens to be.
    day = sa.cast(sa.func.timezone("UTC", Classification.created_at), sa.Date)
    tokens = {
        "input": "input_tokens",
        "output": "output_tokens",
        "cache_write": "cache_creation_input_tokens",
        "cache_read": "cache_read_input_tokens",
    }
    spend_rows = (
        await session.execute(
            # Spend follows the documents it was spent on. A household should
            # not be shown, or billed against, another household's API usage.
            sa.select(
                day.label("day"),
                Classification.model,
                *[
                    sa.func.coalesce(
                        sa.func.sum(
                            sa.cast(Classification.usage[key].astext, sa.BigInteger)
                        ),
                        0,
                    ).label(kind)
                    for kind, key in tokens.items()
                ],
            )
            .join(Document, Document.id == Classification.document_id)
            .where(
                Classification.created_at >= now - timedelta(days=30),
                Document.library_id.in_(library_ids) if scoped else sa.true(),
            )
            .group_by(day, Classification.model)
        )
    ).all()
    by_day: dict[str, float] = {}
    total_spend = 0.0
    for row in spend_rows:
        # Costed at the rate of whatever model actually ran, not whatever is
        # configured now — switching to Haiku does not make last month cheaper.
        usage = {key: int(getattr(row, kind) or 0) for kind, key in tokens.items()}
        cost = estimate_cost(usage, row.model)
        total_spend += cost
        stamp = row.day.isoformat()
        by_day[stamp] = by_day.get(stamp, 0.0) + cost

    if dead_letter:
        alerts.append(
            Alert(
                "critical", "dead_letter",
                f"{dead_letter} documents gave up and will not retry on their own. "
                "Fix or acknowledge them.",
                {"count": dead_letter},
            )
        )
    if stalled:
        alerts.append(
            Alert(
                "critical", "stalled",
                "Work is queued and nothing is running. The pipeline has stopped.",
                {"queued_for_seconds": oldest_seconds},
            )
        )
    if stuck_jobs:
        alerts.append(
            Alert(
                "critical", "stuck",
                f"{len(stuck_jobs)} jobs have held a lock past the lease their "
                "stage allows. A worker probably died without releasing them.",
                {"count": len(stuck_jobs)},
            )
        )
    if files_by_state.get(str(SourceFileState.FAILED)):
        alerts.append(
            Alert(
                "warning", "failed_files",
                f"{files_by_state[str(SourceFileState.FAILED)]} files failed to process. "
                "They are still stored — nothing was lost.",
                {"count": files_by_state[str(SourceFileState.FAILED)]},
            )
        )
    if failed_24h:
        alerts.append(
            Alert(
                "warning", "recent_failures",
                f"{failed_24h} jobs failed in the last day.",
                {"count": failed_24h},
            )
        )

    alerts.extend(await _offsite_alerts(session, now))

    return HealthPanel(
        checked_at=now,
        queue_depth=queue_depth,
        running=running,
        failed_24h=failed_24h,
        dead_letter=dead_letter,
        declined=declined,
        stuck_jobs=stuck_jobs,
        oldest_queued_seconds=oldest_seconds,
        stalled=stalled,
        files_by_state=files_by_state,
        spend_30d_usd=total_spend,
        spend_by_day=[
            {"day": day, "usd": round(cost, 4)} for day, cost in sorted(by_day.items())
        ],
        alerts=alerts,
    )


async def badge_healthy(
    session: AsyncSession, library_ids: list[uuid.UUID] | None = None
) -> bool:
    """`collect(...).healthy`, without paying for the panel that surrounds it.

    The sidebar draws one warning light from this, and it subscribes to the
    `jobs` topic — which the worker publishes on every state change of every
    stage of every file. During an import that is a burst of refreshes per
    second, and each one was answered by `collect`: eight aggregates over `job`,
    every one of them re-evaluating the visibility subquery over
    `job ⋈ source_file ⋈ document`, plus a thirty-day join across
    `classification` and `document` to total up API spend. None of that was
    read. The load was maximal exactly when the archive was busiest.

    So this asks the same question and reads only what the answer turns on: the
    three critical conditions over `job`, in one pass, and the offsite state,
    which is a handful of single-row lookups on tiny tables.

    It is a second definition of `healthy`, which is the risk here — a new
    critical alert could light the panel and leave the badge dark.
    `tests/test_health_badge.py` asserts the two agree, condition by condition,
    so that drift fails the build rather than going unnoticed.
    """
    now = datetime.now(UTC)
    scoped = library_ids is not None
    in_scope = (
        Job.id.in_(repository.visible_job_ids(library_ids)) if scoped else sa.true()
    )

    dead_letter, running, stuck, oldest_queued = (
        await session.execute(
            sa.select(
                sa.func.count().filter(
                    Job.state == JobState.DEAD_LETTER,
                    Job.acknowledged_at.is_(None),
                ),
                sa.func.count().filter(Job.state == JobState.RUNNING),
                sa.func.count().filter(_held_past_its_lease(now)),
                sa.func.min(Job.scheduled_for).filter(Job.state == JobState.QUEUED),
            )
            .select_from(Job)
            .where(in_scope)
        )
    ).one()

    oldest_seconds = (now - oldest_queued).total_seconds() if oldest_queued else None
    stalled = bool(
        oldest_seconds
        and oldest_seconds > STALL_AFTER.total_seconds()
        and running == 0
    )
    if dead_letter or stalled or stuck:
        return False
    return not any(
        alert.severity == "critical" for alert in await _offsite_alerts(session, now)
    )
