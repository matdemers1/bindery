"""Segment: propose document boundaries over a source file.

Reads the word boxes and page text produced upstream, runs every proposer, and
turns the accumulated votes into a set of documents — page ranges over the
original, which is never touched (ADR-001).

Automation is deliberately not load-bearing here. A file that produces no
confident boundary becomes one document spanning every page, which is the right
answer for the overwhelming majority of scans and a harmless starting point for
the rest: the manual editor (REQ-036) is one click away, and Phase 3 adds an LLM
pass over the seams the heuristics could not settle (REQ-035).

Idempotent, with a caveat worth knowing: replaying this stage restores the
*proposed* segmentation, superseding hand-drawn boundaries. It therefore refuses
to run on a file a human has already segmented.
"""

import json
import logging

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api import queue, segments
from api.artifacts import derived_for
from api.db.enums import ActorType, JobStage, SourceFileState
from api.db.models import AuditEvent, SourceFile
from api.forms.registry import active_registry, match_documents
from api.queue import ClaimedJob
from api.segments import SegmentSpec
from worker.ocr.word_boxes import page_text
from worker.segment.heuristics import (
    PROPOSERS,
    Boundary,
    features_from_boxes,
    propose_form_number,
)

log = logging.getLogger("bindery.worker.segment")

# Below the acceptance threshold but above this, a seam is *ambiguous* — enough
# signal to be worth a second opinion, not enough to act on. These are the only
# candidates the model ever sees (REQ-035), which is what keeps segmentation
# cost proportional to difficulty rather than to page count (R-06).
AMBIGUOUS_FLOOR = 0.3
# Pages either side of a seam that the model is shown.
WINDOW_PAGES = 2

# A seam needs this much agreement before it becomes a document boundary:
# **one strong signal, or two weaker ones that agree.**
#
#   blank page / "page 1 of N" reset / form fingerprint   1.0-1.5  → fires alone
#   bare footer number resetting to 1                     0.5      → needs company
#   layout shift                                          0.4      → needs company
#
# Set at 0.9 rather than 1.0 deliberately: at 1.0 no combination of the two weak
# proposers could ever fire, which would make summing their weights pointless.
BOUNDARY_THRESHOLD = 0.9


def assemble(
    boundaries: list[Boundary], page_count: int
) -> tuple[list[tuple[int, list[str]]], list[tuple[int, list[str]]]]:
    """Sum the votes per seam. Returns (accepted, ambiguous).

    Accepted seams become boundaries outright. Ambiguous ones carry real but
    insufficient signal, and are the only candidates worth spending a model call
    on.
    """
    votes: dict[int, list[Boundary]] = {}
    for boundary in boundaries:
        if 2 <= boundary.page <= page_count:  # page 1 is never a boundary
            votes.setdefault(boundary.page, []).append(boundary)

    accepted: list[tuple[int, list[str]]] = []
    ambiguous: list[tuple[int, list[str]]] = []
    for page in sorted(votes):
        at_page = votes[page]
        score = sum(vote.weight for vote in at_page)
        reasons = [vote.reason for vote in at_page]
        if score >= BOUNDARY_THRESHOLD:
            accepted.append((page, reasons))
        elif score >= AMBIGUOUS_FLOOR:
            ambiguous.append((page, reasons))
    return accepted, ambiguous


async def confirm_ambiguous(
    pages: list, ambiguous: list[tuple[int, list[str]]], source_file_id: str
) -> list[tuple[int, list[str]]]:
    """Ask the model about the seams the heuristics could not settle.

    Failure here is not failure of the stage: an unavailable provider means the
    ambiguous seams simply stay unconfirmed, and the file is segmented on the
    heuristics alone. Segmentation is a metadata edit either way, and the manual
    editor is one click from every result.
    """
    if not ambiguous:
        return []

    from worker.ai import get_provider
    from worker.ai.provider import (
        AIProviderError,
        BoundaryRequest,
        BoundaryWindow,
        ProviderUnavailableError,
    )

    provider = await get_provider()
    if not provider.available():
        log.info("no provider available; leaving %s ambiguous seam(s) uncut", len(ambiguous))
        return []

    by_number = {page.number: page for page in pages}
    windows = [
        BoundaryWindow(
            page=page,
            reasons=reasons,
            before=[
                (number, by_number[number].text)
                for number in range(page - WINDOW_PAGES, page)
                if number in by_number
            ],
            after=[
                (number, by_number[number].text)
                for number in range(page, page + WINDOW_PAGES)
                if number in by_number
            ],
        )
        for page, reasons in ambiguous
    ]

    try:
        confirmation = await provider.confirm_boundaries(
            BoundaryRequest(source_file_id=source_file_id, windows=windows)
        )
    except (AIProviderError, ProviderUnavailableError) as exc:
        log.warning("boundary confirmation unavailable (%s); using heuristics alone", exc)
        return []

    reasons_by_page = dict(ambiguous)
    return [
        (verdict.page, [*reasons_by_page.get(verdict.page, []), f"confirmed: {verdict.reason}"])
        for verdict in confirmation.verdicts
        if verdict.starts_new_document and verdict.page in reasons_by_page
    ]


def specs_from_boundaries(boundaries: list[int], page_count: int) -> list[SegmentSpec]:
    starts = [1, *boundaries]
    ends = [page - 1 for page in boundaries] + [page_count]
    return [
        SegmentSpec(page_start=start, page_end=end)
        for start, end in zip(starts, ends, strict=True)
    ]


async def _human_has_segmented(session: AsyncSession, source_file_id) -> bool:
    return (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.entity_type == "source_file",
                AuditEvent.entity_id == source_file_id,
                AuditEvent.action.in_(("segment", "segment_undo")),
                AuditEvent.actor_type == ActorType.HUMAN.value,
            )
        )
    ).scalar_one() > 0


async def run_segment(session: AsyncSession, job: ClaimedJob) -> None:
    source_file = await session.get(SourceFile, job.source_file_id)
    if source_file is None:
        raise ValueError(f"source file {job.source_file_id} no longer exists")

    if await _human_has_segmented(session, source_file.id):
        log.info(
            "%s was segmented by hand; leaving it alone", source_file.original_filename
        )
        source_file.state = SourceFileState.PROCESSED
        # Leaving the boundaries alone is not a reason to leave the documents
        # unindexed. A rescan re-OCRs a bundle a person cut by hand — which is
        # exactly the kind of file people cut by hand — and returning here
        # without the cascade stopped the replay dead: `page.text` carried the
        # new OCR, `document.embedding` still described the old, and no
        # classify or rules job was ever created for it.
        await queue.requeue_stage(session, JobStage.EMBED, source_file_id=source_file.id)
        await session.flush()
        return

    paths = derived_for(source_file.sha256)
    if not paths.word_boxes.is_file():
        raise FileNotFoundError("word boxes are missing; re-run normalize first")

    boxes = json.loads(paths.word_boxes.read_text())
    pages = [features_from_boxes(page, page_text(page)) for page in boxes["pages"]]
    page_count = len(pages)
    if page_count == 0:
        raise ValueError("no pages to segment")

    registry = await active_registry(session)
    votes: list[Boundary] = []
    for proposer in PROPOSERS:
        votes.extend(
            propose_form_number(pages, registry)
            if proposer is propose_form_number
            else proposer(pages)
        )

    accepted, ambiguous = assemble(votes, page_count)
    confirmed = await confirm_ambiguous(pages, ambiguous, str(source_file.id))
    accepted = sorted([*accepted, *confirmed])
    specs = specs_from_boundaries([page for page, _ in accepted], page_count)

    documents = await segments.replace(
        session, source_file, specs, actor_type=ActorType.SYSTEM
    )
    matches = await match_documents(session, documents)
    await _date_from_the_file(session, source_file.id, documents)

    source_file.state = SourceFileState.PROCESSED
    await session.flush()

    # Documents exist now, so they can be embedded and classified.
    # `requeue_stage`, not `enqueue`: enqueue is idempotent and deliberately
    # refuses to disturb an existing job, which is right for the first run and
    # wrong for a replay. On a rescan the downstream job already exists and
    # already succeeded, so `enqueue` is a no-op and the replay stops dead here
    # — the file gets re-OCR'd and nothing downstream ever sees the new text.
    # On a first run there is no existing job, so the two behave identically.
    await queue.requeue_stage(session, JobStage.EMBED, source_file_id=source_file.id)

    named = sum(1 for found in matches.values() if found)
    log.info(
        "segmented %s into %s document(s); %s matched a known form",
        source_file.original_filename, len(documents), named,
    )
    for page, reasons in accepted:
        log.info("  boundary at page %s: %s", page, "; ".join(reasons))


async def _date_from_the_file(session: AsyncSession, source_file_id, documents) -> None:
    """A photograph's capture date fills `document_date` when nothing else has
    (T-18.5, REQ-193), recorded as source `file` so the why-panel can say
    "read from the file's metadata" and so a person's edit still wins.

    Only when nothing else has: a document that already carries a date — from
    a re-run, from a rule, from a person — keeps it. Metadata is a fact about
    the file, not an argument with whoever set the date.
    """
    from datetime import UTC

    from api import field_source
    from api.db.enums import FieldSource as Kind
    from api.db.models import MediaMetadata

    meta = await session.get(MediaMetadata, source_file_id)
    if meta is None or meta.captured_at is None:
        return
    for document in documents:
        if document.document_date is not None:
            continue
        held = await field_source.held_by_human(session, document.id)
        if "document_date" in held:
            continue
        document.document_date = meta.captured_at.astimezone(UTC).date()
        await field_source.record(session, document.id, ["document_date"], Kind.FILE)
