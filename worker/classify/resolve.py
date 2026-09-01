"""Turning a model response into archive rows (REQ-046, REQ-048).

Two rules govern everything here.

**Reused ids resolve by id, and by nothing else.** An `existing_id` is looked up
directly. It is never normalised, lowercased, translated, stemmed, or fuzzily
matched against a name — those are all ways an exact match can be silently
corrupted into a near-duplicate, which is the failure this design exists to
prevent.

**Every returned id is re-validated against the caller's visible libraries.**
The candidates were permission-filtered on the way in, but a model can echo back
an id it was never offered, and an id is a capability. An id that does not
resolve inside the document's library is dropped, not resolved leniently.
"""

import logging
import uuid
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.enums import ActorType, TagSource
from api.db.models import (
    AuditEvent,
    Correspondent,
    Document,
    DocumentTag,
    DocumentType,
    Tag,
)
from api.text import slugify
from worker.ai.provider import ClassificationResult

log = logging.getLogger("bindery.worker.classify")


@dataclass
class Resolution:
    """What actually happened, in enough detail for the gate to read it."""

    correspondent_id: uuid.UUID | None = None
    document_type_id: uuid.UUID | None = None
    tag_ids: list[uuid.UUID] = field(default_factory=list)

    correspondent_was_existing: bool = False
    document_type_was_existing: bool = False
    # True only when every tag came from existing_ids — the "no new taxonomy was
    # invented" signal (REQ-057).
    all_tags_existing: bool = True
    invented_names: list[str] = field(default_factory=list)
    rejected_ids: list[str] = field(default_factory=list)


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def _resolve_existing(
    session: AsyncSession, model, raw_id: str | None, library_ids: list[uuid.UUID]
):
    """Look an id up, scoped. Returns the row, or None if it is not the caller's."""
    parsed = _parse_uuid(raw_id)
    if parsed is None:
        return None
    return (
        await session.execute(
            sa.select(model).where(
                model.id == parsed,
                sa.or_(model.library_id.in_(library_ids), model.library_id.is_(None)),
            )
        )
    ).scalar_one_or_none()


async def _get_or_create(session: AsyncSession, model, name: str, library_id: uuid.UUID):
    """Create a new taxonomy entry, or reuse one that already has this slug.

    The slug check is a last-ditch guard against the model proposing a new name
    that differs only in case or punctuation from something already present.
    """
    slug = slugify(name)
    existing = (
        await session.execute(
            sa.select(model).where(model.library_id == library_id, model.slug == slug)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    created = model(library_id=library_id, name=name.strip(), slug=slug)
    session.add(created)
    await session.flush()
    return created, True


async def resolve(
    session: AsyncSession,
    document: Document,
    result: ClassificationResult,
    library_ids: list[uuid.UUID],
) -> Resolution:
    resolution = Resolution()

    # ---- correspondent -------------------------------------------------
    if result.correspondent.existing_id:
        found = await _resolve_existing(
            session, Correspondent, result.correspondent.existing_id, library_ids
        )
        if found is not None:
            resolution.correspondent_id = found.id
            resolution.correspondent_was_existing = True
        else:
            resolution.rejected_ids.append(f"correspondent:{result.correspondent.existing_id}")
    elif result.correspondent.new_name:
        # Alias resolution first: "Honda Fin Svcs" must land on the existing
        # American Honda Finance rather than creating a near-duplicate.
        from api.entities import resolve_correspondent

        matched = await resolve_correspondent(
            session, result.correspondent.new_name, document.library_id
        )
        if matched is not None:
            resolution.correspondent_id = matched.id
            resolution.correspondent_was_existing = True
            created, is_new = matched, False
        else:
            created, is_new = await _get_or_create(
                session, Correspondent, result.correspondent.new_name, document.library_id
            )
        resolution.correspondent_id = created.id
        resolution.correspondent_was_existing = not is_new
        if is_new:
            resolution.invented_names.append(f"correspondent:{created.name}")

    # ---- document type -------------------------------------------------
    if result.document_type.existing_id:
        found = await _resolve_existing(
            session, DocumentType, result.document_type.existing_id, library_ids
        )
        if found is not None:
            resolution.document_type_id = found.id
            resolution.document_type_was_existing = True
        else:
            resolution.rejected_ids.append(f"document_type:{result.document_type.existing_id}")
    elif result.document_type.new_name:
        created, is_new = await _get_or_create(
            session, DocumentType, result.document_type.new_name, document.library_id
        )
        resolution.document_type_id = created.id
        resolution.document_type_was_existing = not is_new
        if is_new:
            resolution.invented_names.append(f"document_type:{created.name}")

    # ---- tags ----------------------------------------------------------
    for raw_id in result.tags.existing_ids:
        found = await _resolve_existing(session, Tag, raw_id, library_ids)
        if found is not None:
            resolution.tag_ids.append(found.id)
        else:
            resolution.rejected_ids.append(f"tag:{raw_id}")
            # An id that does not resolve is not a reused tag, so the document
            # can no longer claim it invented nothing.
            resolution.all_tags_existing = False

    for name in result.tags.new_names:
        created, is_new = await _get_or_create(session, Tag, name, document.library_id)
        resolution.tag_ids.append(created.id)
        if is_new:
            resolution.all_tags_existing = False
            resolution.invented_names.append(f"tag:{created.name}")

    if resolution.rejected_ids:
        log.warning(
            "document %s: dropped %s id(s) the model returned that are not visible here: %s",
            document.id, len(resolution.rejected_ids), ", ".join(resolution.rejected_ids),
        )
    return resolution


async def removed_by_human(
    session: AsyncSession, document_id: uuid.UUID
) -> set[uuid.UUID]:
    """Tags a person took off this document (T-17.6, REQ-191).

    Taking a tag off is a decision, and re-applying it on the next
    classification run is the same silent revert that overwriting a corrected
    title would be. The link row survives its removal — that is the whole point
    of `removed_at` — so the record of the decision is already there to read.
    """
    rows = await session.execute(
        sa.select(DocumentTag.tag_id)
        .join(AuditEvent, AuditEvent.id == DocumentTag.removed_by_event_id)
        .where(
            DocumentTag.document_id == document_id,
            DocumentTag.removed_at.is_not(None),
            AuditEvent.actor_type == ActorType.HUMAN,
        )
    )
    return set(rows.scalars().all())


async def apply_tags(
    session: AsyncSession,
    document: Document,
    tag_ids: list[uuid.UUID],
    source: TagSource,
    *,
    skip: set[uuid.UUID] | None = None,
) -> None:
    """Attach tags, recording provenance on the link row itself (REQ-078).

    Existing links are left alone rather than rewritten: a tag a human set must
    not silently become an AI-sourced one because the classifier agreed with it.

    `skip` carries the tags a person has removed. Without it the model re-adds
    them on the next run, which is the same silent revert this phase exists to
    prevent — and the more infuriating version of it, because removing the tag
    again does nothing.
    """
    # Every link, live or revoked. `document_tag` is keyed on
    # `(document_id, tag_id)`, so a revoked link still occupies the row — and
    # looking only at live ones means re-applying a revoked tag INSERTs over an
    # existing primary key and raises. Reachable today: undo revokes a
    # classification's tags, and the next run tries to put them back.
    rows = (
        await session.execute(
            sa.select(DocumentTag).where(DocumentTag.document_id == document.id)
        )
    ).scalars().all()
    links = {row.tag_id: row for row in rows}
    live = {tag_id for tag_id, row in links.items() if row.removed_at is None}
    refused = skip or set()

    for tag_id in tag_ids:
        if tag_id in live or tag_id in refused:
            continue
        if (revoked := links.get(tag_id)) is not None:
            # Revived rather than re-inserted, and it keeps the source it was
            # first given: a tag a human set must not silently become
            # AI-sourced because the classifier later agreed with it.
            revoked.removed_at = None
            revoked.removed_by_event_id = None
        else:
            session.add(
                DocumentTag(document_id=document.id, tag_id=tag_id, source=source)
            )
        live.add(tag_id)
    await session.flush()
