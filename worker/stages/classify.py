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
from worker.ai.provider import PageImage
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



# Enough characters that the page plainly carried readable text. A handful of
# stray marks misread as letters is not text worth classifying from.
MIN_USABLE_TEXT = 40

# Images are an order of magnitude more expensive than text, and a document
# whose first pages say nothing is not usually saved by its twentieth.
MAX_PAGE_IMAGES = 3


def _has_text(pages: list[tuple[int, str]]) -> bool:
    return sum(len((text or "").strip()) for _, text in pages) >= MIN_USABLE_TEXT


def _page_images(source_file: SourceFile | None, document: Document) -> list[PageImage]:
    """The rendered pages, for a document with nothing readable on it."""
    if source_file is None:
        return []
    artifacts = derived_for(source_file.sha256)
    images: list[PageImage] = []
    for page_number in range(document.page_start, document.page_end + 1):
        if len(images) >= MAX_PAGE_IMAGES:
            break
        # The thumbnail, not the full render: a page is identifiable from it,
        # and a full-resolution scan would be megabytes of tokens per page.
        path = artifacts.page_thumb(page_number)
        if not path.is_file():
            continue
        try:
            images.append(
                PageImage(
                    page_number=page_number,
                    media_type="image/webp",
                    data=path.read_bytes(),
                )
            )
        except OSError as error:
            log.warning("could not read render for page %s: %s", page_number, error)
    return images


async def run_classify(session: AsyncSession, job: ClaimedJob) -> None:
    document = await session.get(Document, job.document_id)
    if document is None or document.superseded_at is not None:
        # The boundaries moved under us. The replacement document has its own
        # job; this one has nothing left to classify.
        log.info("document %s is no longer live; skipping", job.document_id)
        return

    provider = await get_provider(session)
    library_ids = [document.library_id]
    source_file = await session.get(SourceFile, document.source_file_id)

    known_form = (
        await session.get(KnownForm, document.known_form_id)
        if document.known_form_id
        else None
    )
    candidate_set = await candidate_builder.build(session, document, library_ids)

    pages = await _pages(session, document)

    # A document OCR could not read is not a document nothing can be said
    # about. It is usually a photograph, a patch, a diagram or a signature
    # page — things a person identifies at a glance and a text pipeline
    # cannot. The rendered pages go to the model only in that case, because
    # images cost far more than text and add nothing when the text is good.
    page_images = _page_images(source_file, document) if not _has_text(pages) else []

    request = ClassificationRequest(
        document_id=str(document.id),
        pages=pages,
        page_images=page_images,
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
        is_backlog=document.is_backlog,
    )
    verdict = gate_module.decide(signals)

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
