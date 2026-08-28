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

from api import segments
from api.artifacts import derived_for
from api.db.enums import ActorType, SourceFileState
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


def assemble(boundaries: list[Boundary], page_count: int) -> list[tuple[int, list[str]]]:
    """Sum the votes per seam and keep the ones that clear the threshold."""
    votes: dict[int, list[Boundary]] = {}
    for boundary in boundaries:
        if 2 <= boundary.page <= page_count:  # page 1 is never a boundary
            votes.setdefault(boundary.page, []).append(boundary)

    accepted: list[tuple[int, list[str]]] = []
    for page in sorted(votes):
        at_page = votes[page]
        if sum(vote.weight for vote in at_page) >= BOUNDARY_THRESHOLD:
            accepted.append((page, [vote.reason for vote in at_page]))
    return accepted


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

    accepted = assemble(votes, page_count)
    specs = specs_from_boundaries([page for page, _ in accepted], page_count)

    documents = await segments.replace(
        session, source_file, specs, actor_type=ActorType.SYSTEM
    )
    matches = await match_documents(session, documents)

    source_file.state = SourceFileState.PROCESSED
    await session.flush()

    named = sum(1 for found in matches.values() if found)
    log.info(
        "segmented %s into %s document(s); %s matched a known form",
        source_file.original_filename, len(documents), named,
    )
    for page, reasons in accepted:
        log.info("  boundary at page %s: %s", page, "; ".join(reasons))
