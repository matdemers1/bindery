"""Batch classification (REQ-054). Half price, and the right tool for a backlog.

> Results arrive in **any order** — key by `custom_id`, never by position.

That is not a style note. Matching by position silently attaches one document's
title, date and tags to a different document. In a system whose kill criterion
is loss of trust, a scrambled backlog is the worst available failure: it is
wrong everywhere, and nothing about it looks wrong.

So `custom_id` is a stored row (`batch_request`), written before submission and
looked up on return. A result whose `custom_id` we did not send is dropped and
logged, never guessed at.
"""

import json
import logging
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import BatchRequest, BatchSubmission
from worker.ai.provider import ClassificationRequest, ClassificationResult

log = logging.getLogger("bindery.worker.batch")

# Anthropic's per-batch ceiling is far higher; this keeps a failure's blast
# radius small and progress visible.
MAX_PER_BATCH = 500


@dataclass
class BatchOutcome:
    custom_id: str
    document_id: uuid.UUID
    result: ClassificationResult | None
    error: str | None


def custom_id_for(document_id: uuid.UUID) -> str:
    """Stable, and reversible only through the stored row — not by parsing."""
    return f"doc-{document_id}"


async def submit(
    session: AsyncSession,
    client,
    *,
    model: str,
    prompt_version: str,
    system_blocks: list[dict],
    requests: list[tuple[uuid.UUID, ClassificationRequest, str]],
    import_session_id: uuid.UUID | None = None,
    max_tokens: int = 8000,
) -> BatchSubmission:
    """Send a batch and record every custom_id before it leaves.

    The rows are written *first*. If submission fails we have an accurate record
    of what was attempted; if it succeeds we can reconcile. The reverse order
    would leave results we cannot attribute.
    """
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    submission = BatchSubmission(
        session_id=import_session_id,
        provider_batch_id="pending",
        prompt_version=prompt_version,
        request_count=len(requests),
    )
    session.add(submission)
    await session.flush()

    payload = []
    for document_id, _, user_content in requests:
        custom_id = custom_id_for(document_id)
        session.add(
            BatchRequest(
                submission_id=submission.id, custom_id=custom_id, document_id=document_id
            )
        )
        payload.append(
            Request(
                custom_id=custom_id,
                params=MessageCreateParamsNonStreaming(
                    model=model,
                    max_tokens=max_tokens,
                    system=system_blocks,
                    messages=[{"role": "user", "content": user_content}],
                    output_config={
                        "format": {
                            "type": "json_schema",
                            "schema": ClassificationResult.model_json_schema(),
                        }
                    },
                ),
            )
        )
    await session.flush()

    batch = await client.messages.batches.create(requests=payload)
    submission.provider_batch_id = batch.id
    submission.state = batch.processing_status
    await session.flush()

    log.info("submitted batch %s with %s requests", batch.id, len(payload))
    return submission


async def poll(session: AsyncSession, client, submission: BatchSubmission) -> str:
    batch = await client.messages.batches.retrieve(submission.provider_batch_id)
    submission.state = batch.processing_status
    await session.flush()
    return batch.processing_status


async def reconcile(
    session: AsyncSession, client, submission: BatchSubmission
) -> list[BatchOutcome]:
    """Match results to documents by `custom_id`, and by nothing else."""
    rows = (
        await session.execute(
            sa.select(BatchRequest).where(BatchRequest.submission_id == submission.id)
        )
    ).scalars().all()
    by_custom_id = {row.custom_id: row for row in rows}

    outcomes: list[BatchOutcome] = []
    succeeded = errored = 0

    async for entry in await client.messages.batches.results(submission.provider_batch_id):
        row = by_custom_id.get(entry.custom_id)
        if row is None:
            # A result for something we did not send. Never guessed at.
            log.error(
                "batch %s returned an unknown custom_id %r; dropping it",
                submission.provider_batch_id, entry.custom_id,
            )
            continue

        if entry.result.type != "succeeded":
            errored += 1
            row.state = entry.result.type
            row.error = str(getattr(entry.result, "error", entry.result.type))[:500]
            outcomes.append(
                BatchOutcome(entry.custom_id, row.document_id, None, row.error)
            )
            continue

        try:
            text = next(
                block.text for block in entry.result.message.content if block.type == "text"
            )
            result = ClassificationResult.model_validate(json.loads(text))
        except Exception as exc:
            # A malformed response is never partially applied (REQ-045).
            errored += 1
            row.state = "invalid"
            row.error = f"response did not match the schema: {exc}"[:500]
            outcomes.append(BatchOutcome(entry.custom_id, row.document_id, None, row.error))
            continue

        succeeded += 1
        row.state = "succeeded"
        outcomes.append(BatchOutcome(entry.custom_id, row.document_id, result, None))

    submission.succeeded_count = succeeded
    submission.errored_count = errored
    submission.state = "reconciled"
    await session.flush()

    unaccounted = len(rows) - len(outcomes)
    if unaccounted:
        # Silence about missing documents is how a backlog quietly half-imports.
        log.warning(
            "batch %s: %s request(s) had no result at all", submission.provider_batch_id,
            unaccounted,
        )
    log.info("batch %s reconciled: %s ok, %s errored", submission.provider_batch_id,
             succeeded, errored)
    return outcomes
