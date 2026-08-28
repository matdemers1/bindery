"""Moving a file between libraries (T-7.3, REQ-102).

Cross-library search is deliberately impossible, which has a sharp consequence:
**a document filed to the wrong library is invisible, not merely misfiled.** So
moving has to be an obvious, audited action rather than a quiet correction.

Two things about the shape of a move follow from the schema rather than from
preference:

**The unit is the source file, not the document.** A document's library is tied
to its file's by a composite foreign key — `(source_file_id, library_id)` must
match a real `source_file(id, library_id)` row. That constraint exists so a
segment can never drift into a library its pages are not in, and it means a
single document inside a twelve-page scan cannot be moved on its own. Moving the
file moves every document in it, and the API says so instead of pretending
otherwise.

**Taxonomy does not travel.** Tags, correspondents and types belong to a
library. A move that carried them would either duplicate them into the target or
leave rows pointing across the boundary the move exists to respect. So
cross-library references are cleared, recorded in full in the audit `before`,
and reported back to the caller — losing a tag silently would be worse than the
misfiling being corrected.
"""

import logging
import uuid
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit import record
from api.db.enums import ActorType
from api.db.models import (
    Correspondent,
    Document,
    DocumentTag,
    DocumentType,
    SourceFile,
    Tag,
    live_tag_links,
)

log = logging.getLogger("bindery.moves")


@dataclass
class MovePlan:
    """What a move would do, in the caller's words rather than in row counts."""

    source_file_id: uuid.UUID
    from_library_id: uuid.UUID
    to_library_id: uuid.UUID
    document_count: int
    documents: list[dict] = field(default_factory=list)
    cleared_correspondents: list[str] = field(default_factory=list)
    cleared_types: list[str] = field(default_factory=list)
    cleared_tags: list[str] = field(default_factory=list)

    @property
    def loses_metadata(self) -> bool:
        return bool(self.cleared_correspondents or self.cleared_types or self.cleared_tags)

    def as_dict(self) -> dict:
        return {
            "source_file_id": str(self.source_file_id),
            "from_library_id": str(self.from_library_id),
            "to_library_id": str(self.to_library_id),
            "document_count": self.document_count,
            "documents": self.documents,
            "cleared_correspondents": self.cleared_correspondents,
            "cleared_types": self.cleared_types,
            "cleared_tags": self.cleared_tags,
        }


async def plan(
    session: AsyncSession, source_file: SourceFile, to_library_id: uuid.UUID
) -> MovePlan:
    """Work out what a move costs, without doing it.

    Previewing matters more here than elsewhere: the caller is about to make a
    set of documents disappear from one person's view and appear in another's.
    """
    documents = (
        (
            await session.execute(
                sa.select(Document).where(Document.source_file_id == source_file.id)
            )
        )
        .scalars()
        .all()
    )

    result = MovePlan(
        source_file_id=source_file.id,
        from_library_id=source_file.library_id,
        to_library_id=to_library_id,
        document_count=len(documents),
        documents=[
            {
                "id": str(document.id),
                "title": document.title,
                "page_start": document.page_start,
                "page_end": document.page_end,
            }
            for document in documents
        ],
    )
    if not documents:
        return result

    document_ids = [document.id for document in documents]

    correspondent_ids = {d.correspondent_id for d in documents if d.correspondent_id}
    if correspondent_ids:
        result.cleared_correspondents = list(
            (
                await session.execute(
                    sa.select(Correspondent.name).where(
                        Correspondent.id.in_(correspondent_ids),
                        Correspondent.library_id != to_library_id,
                    )
                )
            )
            .scalars()
            .all()
        )

    type_ids = {d.document_type_id for d in documents if d.document_type_id}
    if type_ids:
        result.cleared_types = list(
            (
                await session.execute(
                    sa.select(DocumentType.name).where(
                        DocumentType.id.in_(type_ids),
                        DocumentType.library_id != to_library_id,
                    )
                )
            )
            .scalars()
            .all()
        )

    result.cleared_tags = list(
        (
            await session.execute(
                sa.select(Tag.name)
                .join(DocumentTag, DocumentTag.tag_id == Tag.id)
                .where(
                    DocumentTag.document_id.in_(document_ids),
                    live_tag_links(),
                    Tag.library_id != to_library_id,
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    return result


async def move(
    session: AsyncSession,
    source_file: SourceFile,
    to_library_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
) -> MovePlan:
    """Move a file and everything in it, in one audited transaction."""
    before = await plan(session, source_file, to_library_id)
    if before.from_library_id == to_library_id:
        return before

    # `fk_document_source_file_library` ties a document's library to its file's,
    # and a move must change both. There is no row order that satisfies it
    # midway — documents-first breaks the child side, file-first breaks the
    # parent side — so the check is deferred to commit. The invariant is not
    # weakened: it still holds at every point anyone outside this transaction
    # can observe. See migration 0009.
    await session.execute(
        sa.text("SET CONSTRAINTS fk_document_source_file_library DEFERRED")
    )

    documents = (
        (
            await session.execute(
                sa.select(Document).where(Document.source_file_id == source_file.id)
            )
        )
        .scalars()
        .all()
    )
    document_ids = [document.id for document in documents]

    # Tag links are revoked, not deleted — `removed_at` keeps the history of
    # what this document used to be tagged with, which is the whole point of
    # supersede-never-delete (REQ-090).
    if document_ids:
        stale_links = (
            (
                await session.execute(
                    sa.select(DocumentTag)
                    .join(Tag, Tag.id == DocumentTag.tag_id)
                    .where(
                        DocumentTag.document_id.in_(document_ids),
                        live_tag_links(),
                        Tag.library_id != to_library_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for link in stale_links:
            link.removed_at = sa.func.now()

    valid_correspondents = set(
        (
            await session.execute(
                sa.select(Correspondent.id).where(Correspondent.library_id == to_library_id)
            )
        )
        .scalars()
        .all()
    )
    valid_types = set(
        (
            await session.execute(
                sa.select(DocumentType.id).where(DocumentType.library_id == to_library_id)
            )
        )
        .scalars()
        .all()
    )

    for document in documents:
        if document.correspondent_id and document.correspondent_id not in valid_correspondents:
            document.correspondent_id = None
        if document.document_type_id and document.document_type_id not in valid_types:
            document.document_type_id = None
        # The composite FK requires the file to move first in the same flush,
        # so both sides are updated before anything is written out.
        document.library_id = to_library_id

    source_file.library_id = to_library_id

    await record(
        session,
        entity_type="source_file",
        entity_id=source_file.id,
        action="moved_library",
        actor_type=ActorType.HUMAN,
        actor_id=actor_id,
        before={
            "library_id": str(before.from_library_id),
            "correspondents": before.cleared_correspondents,
            "document_types": before.cleared_types,
            "tags": before.cleared_tags,
        },
        after=before.as_dict(),
    )
    await session.flush()

    log.info(
        "moved source file %s from %s to %s (%s documents, %s tags cleared)",
        source_file.id, before.from_library_id, to_library_id,
        before.document_count, len(before.cleared_tags),
    )
    return before
