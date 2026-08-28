"""Classify: ask the model, resolve by id, record why (REQ-045, REQ-049).

The order is deliberate. Candidates are built and permission-filtered *before*
the prompt; the response is validated against a strict schema; ids are
re-validated *after*; provenance is written in the same transaction as the
values it justifies. Retrofitting provenance later would mean re-running
everything, and a value without its justification is exactly the thing the
why-panel cannot show.

If the provider is unavailable the job fails and retries with backoff. The
document remains OCR'd, paged, segmented, and **fully searchable** throughout —
retrieval never depends on this stage (invariant 7, REQ-055).
"""

import json
import logging

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api import queue
from api.artifacts import derived_for, relative_to_data
from api.db.enums import ActorType, JobStage, ReviewState, TagSource
from api.db.models import Classification, Document, FieldProvenance, KnownForm, Page, SourceFile
from api.queue import ClaimedJob
from worker.ai import ClassificationRequest, get_provider
from worker.classify import candidates as candidate_builder
from worker.classify import gate as gate_module
from worker.classify.resolve import apply_tags, resolve

log = logging.getLogger("bindery.worker.classify")


async def _pages(session: AsyncSession, document: Document) -> list[tuple[int, str]]:
    rows = (
        await session.execute(
            sa.select(Page.page_number, Page.text)
            .where(
                Page.source_file_id == document.source_file_id,
                Page.page_number >= document.page_start,
                Page.page_number <= document.page_end,
            )
            .order_by(Page.page_number)
        )
    ).all()
    # Numbered within the document, so evidence pages the model returns are
    # already in the frame a reader sees (REQ-030).
    return [(index, text or "") for index, (_, text) in enumerate(rows, start=1)]


def _absolute_page(document: Document, document_page: int | None) -> int | None:
    if document_page is None:
        return None
    absolute = document.page_start + document_page - 1
    return absolute if document.page_start <= absolute <= document.page_end else None


async def run_classify(session: AsyncSession, job: ClaimedJob) -> None:
    document = await session.get(Document, job.document_id)
    if document is None or document.superseded_at is not None:
        # The boundaries moved under us. The replacement document has its own
        # job; this one has nothing left to classify.
        log.info("document %s is no longer live; skipping", job.document_id)
        return

    provider = await get_provider(session)
    library_ids = [document.library_id]

    known_form = (
        await session.get(KnownForm, document.known_form_id)
        if document.known_form_id
        else None
    )
    candidate_set = await candidate_builder.build(session, document, library_ids)

    request = ClassificationRequest(
        document_id=str(document.id),
        pages=await _pages(session, document),
        correspondents=candidate_set.correspondents,
        document_types=candidate_set.document_types,
        tags=candidate_set.tags,
        known_form_code=known_form.code if known_form else None,
        known_form_fields=list((known_form.field_extractors or {}).keys())
        if known_form
        else [],
    )

    # Raises ProviderUnavailableError or AIProviderError — both retried by the
    # queue, neither silently swallowed.
    response = await provider.classify(request)
    result = response.result

    resolution = await resolve(session, document, result, library_ids)

    document.title = result.title or document.title
    document.summary = result.summary or document.summary
    document.correspondent_id = resolution.correspondent_id or document.correspondent_id
    document.document_type_id = resolution.document_type_id or document.document_type_id
    if result.document_date:
        try:
            from datetime import date

            document.document_date = date.fromisoformat(result.document_date)
        except ValueError:
            # A malformed date is dropped rather than stored: a wrong date
            # nobody will check is worse than no date (REQ-052).
            log.warning("document %s: unusable date %r", document.id, result.document_date)

    await apply_tags(session, document, resolution.tag_ids, TagSource.AI)

    signals = gate_module.collect(
        result,
        resolution,
        known_form_match=known_form is not None,
        rule_fired=False,  # rules run in their own stage, after this one
        neighbour_similarity=candidate_set.best_similarity,
    )
    verdict = gate_module.decide(signals)

    source_file = await session.get(SourceFile, document.source_file_id)
    artifacts = derived_for(source_file.sha256) if source_file else None
    request_path = response_path = None
    if artifacts is not None:
        directory = artifacts.root / "classify"
        directory.mkdir(parents=True, exist_ok=True)
        stem = f"{document.id}-{response.prompt_version}"
        request_file = directory / f"{stem}.request.json"
        response_file = directory / f"{stem}.response.json"
        request_file.write_text(json.dumps(response.raw_request, indent=1, default=str))
        response_file.write_text(json.dumps(response.raw_response, indent=1, default=str))
        request_path = relative_to_data(request_file)
        response_path = relative_to_data(response_file)

    classification = Classification(
        document_id=document.id,
        model=response.model,
        prompt_version=response.prompt_version,
        request_artifact_path=request_path,
        response_artifact_path=response_path,
        confidence=dict(result.confidence),
        structural_signals=signals.to_json(),
        gate_decision=verdict.decision.value,
        gate_reasons=verdict.reasons,
        extracted_fields=dict(result.extracted_fields),
        usage=response.usage,
    )
    session.add(classification)
    await session.flush()

    # Provenance in the same transaction as the values it justifies.
    for evidence in result.evidence:
        session.add(
            FieldProvenance(
                classification_id=classification.id,
                field_name=evidence.field,
                page_number=_absolute_page(document, evidence.page),
                snippet=evidence.snippet,
                confidence=result.confidence.get(evidence.field),
            )
        )

    document.review_state = ReviewState.PENDING_CLASSIFICATION
    await session.flush()

    from api.audit import record

    await record(
        session,
        entity_type="document",
        entity_id=document.id,
        action="classify",
        actor_type=ActorType.AI,
        after={
            "title": document.title,
            "prompt_version": response.prompt_version,
            "gate_decision": verdict.decision.value,
        },
    )

    # Rules get the last word, and the gate is re-run once they have spoken.
    await queue.enqueue(session, JobStage.RULES, document_id=document.id)
    log.info(
        "classified %s -> %r (gate: %s, score %.1f)",
        document.id, document.title, verdict.decision.value, verdict.score,
    )
