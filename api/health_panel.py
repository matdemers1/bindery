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

from api.db.enums import JobState, SourceFileState
from api.db.models import Classification, Job, SourceFile

log = logging.getLogger("bindery.health")

# A job holding a lock longer than this is not slow, it is gone: the worker that
# claimed it died without releasing. OCR on a 400-page scan is the slowest real
# stage and finishes well inside this.
STALE_LOCK = timedelta(minutes=30)

# Work queued this long with nothing running means the pipeline has stopped,
# even though nothing has failed.
STALL_AFTER = timedelta(minutes=15)

# Published per-million-token prices. Approximate by construction — the point is
# to notice a runaway loop, not to reconcile an invoice.
PRICE_PER_MTOK = {
    "input": 5.0,
    "output": 25.0,
    "cache_write": 6.25,
    "cache_read": 0.5,
}


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


def estimate_cost(usage: dict) -> float:
    """Dollars from one classification's token usage.

    Cache reads are an order of magnitude cheaper than fresh input, so counting
    them as input would make a healthy cache look like a spending problem.
    """
    if not usage:
        return 0.0
    tokens = {
        "input": usage.get("input_tokens", 0),
        "output": usage.get("output_tokens", 0),
        "cache_write": usage.get("cache_creation_input_tokens", 0),
        "cache_read": usage.get("cache_read_input_tokens", 0),
    }
    return sum(count / 1_000_000 * PRICE_PER_MTOK[kind] for kind, count in tokens.items())


async def collect(
    session: AsyncSession, library_ids: list[uuid.UUID] | None = None
) -> HealthPanel:
    now = datetime.now(UTC)
    alerts: list[Alert] = []

    state_rows = (
        await session.execute(
            sa.select(Job.state, Job.stage, sa.func.count()).group_by(Job.state, Job.stage)
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
            .where(Job.state == JobState.FAILED, Job.updated_at >= now - timedelta(days=1))
        )
    ) or 0
    dead_letter = (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(Job)
            .where(Job.state == JobState.DEAD_LETTER)
        )
    ) or 0

    # A worker that died mid-job leaves the lock behind. Nothing fails; the
    # document simply never finishes.
    stuck = (
        (
            await session.execute(
                sa.select(Job)
                .where(
                    Job.state == JobState.RUNNING,
                    Job.locked_at.is_not(None),
                    Job.locked_at < now - STALE_LOCK,
                )
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
        sa.select(sa.func.min(Job.scheduled_for)).where(Job.state == JobState.QUEUED)
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
            sa.select(SourceFile.state, sa.func.count()).group_by(SourceFile.state)
        )
    ).all()
    files_by_state = {str(state): count for state, count in file_rows}

    spend_rows = (
        await session.execute(
            sa.select(Classification.created_at, Classification.usage).where(
                Classification.created_at >= now - timedelta(days=30)
            )
        )
    ).all()
    by_day: dict[str, float] = {}
    total_spend = 0.0
    for created_at, usage in spend_rows:
        cost = estimate_cost(usage or {})
        total_spend += cost
        day = created_at.date().isoformat()
        by_day[day] = by_day.get(day, 0.0) + cost

    if dead_letter:
        alerts.append(
            Alert(
                "critical", "dead_letter",
                f"{dead_letter} documents gave up and will not retry on their own.",
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
                f"{len(stuck_jobs)} jobs have held a lock for over 30 minutes. "
                "A worker probably died without releasing them.",
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

    return HealthPanel(
        checked_at=now,
        queue_depth=queue_depth,
        running=running,
        failed_24h=failed_24h,
        dead_letter=dead_letter,
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
