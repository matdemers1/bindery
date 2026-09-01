"""Who last set each document field, and what classification may therefore write.

The whole of Phase 17 rests on one rule:

    A field a person set is theirs. The model does not get to disagree with
    them three days later.

Without it, correcting a date and then letting AI review run puts the wrong date
back. Nothing fails, nothing is logged, and the archive quietly disagrees with
you — which teaches you not to bother correcting anything. That silent revert,
rather than a bad edit, is the failure this module prevents. Bad edits undo.

Not to be confused with `field_provenance`, which records the page and snippet
justifying an AI-written value. That is provenance of *evidence*; this is
provenance of *authority*. A document can have excellent evidence for a value a
person has since overruled, and only authority can answer "may I write this".
"""

import logging
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.enums import FieldSource as Source
from api.db.models import FieldSource

log = logging.getLogger("bindery.fields")

# The fields a person may set, and therefore the fields that can be held.
# Deliberately the soft, model-suggested ones. `known_form_id` is absent on
# purpose: it feeds the auto-file gate, which is a pure function of *structural*
# signals (invariant 5), and a hand-set form would make an opinion look like a
# registry match to the thing deciding what files itself.
EDITABLE = (
    "title",
    "summary",
    "document_date",
    "correspondent_id",
    "document_type_id",
)


async def held_by_human(
    session: AsyncSession, document_id: uuid.UUID
) -> set[str]:
    """Field names on this document that a person has set.

    An absent row means nobody has claimed the field, which is the right answer
    for every document that existed before this table did — classification
    carries on writing them, exactly as it always has.
    """
    rows = await session.execute(
        sa.select(FieldSource.field_name).where(
            FieldSource.document_id == document_id,
            FieldSource.source == Source.HUMAN,
            FieldSource.released_at.is_(None),
        )
    )
    return set(rows.scalars().all())


async def sources_for(
    session: AsyncSession, document_id: uuid.UUID
) -> dict[str, FieldSource]:
    """Every recorded source for one document, for the why-panel (REQ-064)."""
    rows = await session.execute(
        sa.select(FieldSource).where(
            FieldSource.document_id == document_id,
            FieldSource.released_at.is_(None),
        )
    )
    return {row.field_name: row for row in rows.scalars().all()}


async def record(
    session: AsyncSession,
    document_id: uuid.UUID,
    field_names: list[str],
    source: Source,
    *,
    actor_id: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
) -> None:
    """Claim these fields for `source`.

    Upserted rather than inserted, because setting a field is something that
    happens repeatedly and the table holds the *current* answer. History lives
    in the audit trail, which is where the rest of the project keeps it — the
    same grain as `document.title` itself.
    """
    if not field_names:
        return
    now = sa.func.now()
    for name in field_names:
        await session.execute(
            pg_insert(FieldSource)
            .values(
                document_id=document_id,
                field_name=name,
                source=source,
                set_by=actor_id,
                set_by_event_id=event_id,
                set_at=now,
            )
            .on_conflict_do_update(
                index_elements=[FieldSource.document_id, FieldSource.field_name],
                set_={
                    "source": source,
                    "set_by": actor_id,
                    "set_by_event_id": event_id,
                    "set_at": now,
                    # Re-claiming a released field makes it live again rather
                    # than leaving a tombstone that outranks the new claim.
                    "released_at": None,
                    "released_by_event_id": None,
                },
            )
        )


async def release(
    session: AsyncSession,
    document_id: uuid.UUID,
    field_names: list[str],
    *,
    event_id: uuid.UUID | None = None,
) -> None:
    """Give these fields back, so classification may write them again.

    Used by undo: walking back an edit has to walk back the claim it made, or
    the field stays frozen at a value nobody chose — held by a person whose
    decision has just been reversed.

    Released, not deleted (REQ-090). The row survives, so "somebody set this and
    then took it back" stays answerable, and the guard in
    `tests/test_no_destructive_paths.py` stays satisfied without an exemption.
    """
    if not field_names:
        return
    await session.execute(
        sa.update(FieldSource)
        .where(
            FieldSource.document_id == document_id,
            FieldSource.field_name.in_(field_names),
            FieldSource.released_at.is_(None),
        )
        .values(released_at=sa.func.now(), released_by_event_id=event_id)
    )
