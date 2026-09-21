"""Re-running AI review over documents that never got one (T-8.10).

Two situations produce a document that is filed but unclassified, and they look
identical on screen while meaning completely different things:

- **It was never attempted.** The document arrived before an API key existed.
  Nothing failed; classification simply had nowhere to go. This is the common
  one, because the archive is deliberately useful without a key — OCR, indexing
  and search all work — so people put documents in first and add a key later.
- **It was attempted and gave up.** Five tries, then dead-letter. Either the
  provider was unreachable, or the response could not be used.

The distinction matters because the first is not a failure and should not be
reported as one. "4 documents gave up" is alarming and wrong when the truth is
"4 documents are waiting for the key you just added".

Everything here is a *replay*, not a repair: it resets the classify job and lets
the normal pipeline run. Nothing is deleted, no existing classification is
overwritten until the new one succeeds, and a document whose review you re-run
and which fails again is exactly where it started.
"""

import logging
import uuid
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api import queue, settings_store
from api.db.enums import JobStage, JobState, ReviewState
from api.db.models import Classification, Document, Job
from api.segments import live

log = logging.getLogger("bindery.reclassify")

# The error the worker records when there is no key. Matched as a substring
# because it is the message the provider raises, and the alternative — a
# dedicated column — would be a schema change to carry one bit.
NO_PROVIDER = "no AI provider is configured"


@dataclass
class Reason:
    code: str
    label: str
    detail: str
    document_ids: list[uuid.UUID] = field(default_factory=list)
    # Whether re-running AI review on this bucket could plausibly change anything. False for a
    # refusal: the model already looked and said no, and it will say no again. The screen should
    # not offer an action whose only outcome is the same answer (ADR-011).
    rerunnable: bool = True

    @property
    def count(self) -> int:
        return len(self.document_ids)


@dataclass
class PendingReview:
    reasons: list[Reason]

    @property
    def total(self) -> int:
        return sum(reason.count for reason in self.reasons)

    def all_ids(self) -> list[uuid.UUID]:
        return [i for reason in self.reasons for i in reason.document_ids]

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "reasons": [
                {
                    "code": r.code,
                    "label": r.label,
                    "detail": r.detail,
                    "count": r.count,
                    "rerunnable": r.rerunnable,
                    "document_ids": [str(i) for i in r.document_ids],
                }
                for r in self.reasons
                if r.count
            ],
        }


async def pending(session: AsyncSession, library_ids: list[uuid.UUID]) -> PendingReview:
    """Which documents are waiting for AI review, and why.

    Reads whether a provider key is configured, because two of the three
    reasons below describe what re-running would do, and that answer is
    different when there is nothing to re-run against.
    """
    key_configured = bool(
        await settings_store.get(session, settings_store.ANTHROPIC_API_KEY)
    )
    # The newest classify job per document, so a document that failed and was
    # later re-run is judged on the latest attempt rather than the first.
    latest = (
        sa.select(
            Job.document_id,
            sa.func.max(Job.updated_at).label("updated_at"),
        )
        .where(Job.stage == JobStage.CLASSIFY.value, Job.document_id.is_not(None))
        .group_by(Job.document_id)
        .subquery()
    )
    job = sa.orm.aliased(Job)

    rows = (
        await session.execute(
            sa.select(
                Document.id,
                Document.review_state,
                job.state,
                job.last_error,
                Classification.id.label("classification_id"),
            )
            .outerjoin(
                latest,
                latest.c.document_id == Document.id,
            )
            .outerjoin(
                job,
                sa.and_(
                    job.document_id == Document.id,
                    job.stage == JobStage.CLASSIFY.value,
                    job.updated_at == latest.c.updated_at,
                ),
            )
            .outerjoin(Classification, Classification.document_id == Document.id)
            .where(Document.library_id.in_(library_ids), live())
        )
    ).all()

    never = Reason(
        "never_attempted",
        "Waiting for AI review",
        "These arrived before an API key was configured, so classification never "
        "ran. Nothing failed — they are searchable, they just have no title, date "
        "or tags yet.",
    )
    # The tail of this sentence used to read "Now that a key is set they will
    # succeed" unconditionally, which is read by a person who is looking at
    # this screen *because* nothing is succeeding — and on an archive with no
    # key, which is the ordinary state, it was simply false. The archive's
    # trust surface asserting a fact it has not checked is the shape of the
    # project's stated kill criterion (D-05).
    unavailable = Reason(
        "provider_unavailable",
        "Gave up because there was no key",
        "Classification was attempted, could not reach a model, and stopped "
        "retrying. "
        + (
            "A key is set now, so re-running them should succeed."
            if key_configured
            else "There is still no key, so re-running them will not help yet — "
            "add one in Settings first. They are searchable either way."
        ),
    )
    failed = Reason(
        "failed",
        "Gave up for another reason",
        "These were attempted and could not be completed. Re-running is safe; if "
        "it fails again the error is on the Pipeline screen.",
    )
    # A declined document is not waiting and did not fail (ADR-011). Without this bucket it fell
    # into "never_attempted", under a heading saying nothing failed, and the screen offered to
    # re-run it against a model that had already refused it — forever, and identically each time.
    declined = Reason(
        "declined",
        "Refused by the model",
        "The model looked at these and declined to classify them — a safety refusal, or a "
        "file with nothing in it to read. Re-running will produce the same answer. They are "
        "searchable, and you can title them by hand.",
        rerunnable=False,
    )

    seen: set[uuid.UUID] = set()
    for row in rows:
        # A document can join several classification rows; count it once.
        if row.id in seen:
            continue
        if row.classification_id is not None:
            continue
        seen.add(row.id)

        # The column round-trips as the enum, but a raw value shows up in
        # queries built from strings, so both spellings are accepted.
        state = row.state
        gave_up = {
            JobState.DEAD_LETTER, JobState.FAILED,
            JobState.DEAD_LETTER.value, JobState.FAILED.value,
        }
        declined_states = {JobState.DECLINED, JobState.DECLINED.value}
        if state in declined_states:
            declined.document_ids.append(row.id)
        elif state in gave_up:
            bucket = unavailable if NO_PROVIDER in (row.last_error or "") else failed
            bucket.document_ids.append(row.id)
        elif row.review_state in (
            ReviewState.PENDING_CLASSIFICATION,
            ReviewState.PENDING_CLASSIFICATION.value,
        ):
            # Only documents whose review state says they are still waiting.
            # "No classification row" alone is far too broad: a document filed
            # by a rule, or titled by hand, has no classification and is not
            # waiting for one — offering to run AI over it would be offering to
            # overwrite a human's work.
            never.document_ids.append(row.id)

    return PendingReview(reasons=[never, unavailable, failed, declined])


async def requeue(
    session: AsyncSession, document_ids: list[uuid.UUID], library_ids: list[uuid.UUID]
) -> int:
    """Reset the classify stage for these documents. Scoped, and idempotent.

    Re-filtered against the caller's libraries rather than trusted from the
    request: an id is a capability, and this one costs money to exercise.
    """
    if not document_ids:
        return 0

    allowed = set(
        (
            await session.execute(
                sa.select(Document.id).where(
                    Document.id.in_(document_ids),
                    Document.library_id.in_(library_ids),
                    live(),
                )
            )
        )
        .scalars()
        .all()
    )

    queued = 0
    for document_id in allowed:
        if await queue.requeue_stage(session, JobStage.CLASSIFY, document_id=document_id):
            queued += 1

    log.info("re-queued classification for %s document(s)", queued)
    return queued
