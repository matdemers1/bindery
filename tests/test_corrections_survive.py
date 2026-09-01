"""A correction has to outlive the next classification run (T-17.5, REQ-191).

This is the whole of Phase 17. Everything else in it — the route, the forms,
the visual distinction — is affordance around this one property.

The failure being designed against is not a bad edit. Bad edits undo. It is the
**silent revert**: you correct a date, AI review runs three days later, and the
date goes back. Nothing failed, nothing was logged, and the archive quietly
disagrees with you — which is how somebody learns that correcting things is not
worth the effort.
"""

import hashlib
import uuid
from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa

from api import field_source
from api.db.enums import ActorType, IngestSource, SourceFileState
from api.db.enums import FieldSource as Kind
from api.db.models import (
    AuditEvent,
    Correspondent,
    Document,
    DocumentTag,
    DocumentType,
    FieldSource,
    Page,
    SourceFile,
    Tag,
)
from api.storage.blobs import blob_path

# The page fixtures and the classify helper are the classify suite's, imported
# as plain values. Its `document` fixture is deliberately *not* imported: a
# fixture brought across modules collides with the parameter of the same name
# in every test that uses it, so this module builds its own.
from tests.test_classify import PAGES, _classify


@pytest.fixture
async def document(session, signed_in):
    """A two-page document, segmented and ready to classify."""
    _, library = await signed_in()
    payload = b"%PDF-" + uuid.uuid4().bytes
    sha = hashlib.sha256(payload).hexdigest()
    path = blob_path(sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)

    source_file = SourceFile(
        library_id=library.id, sha256=sha, byte_size=len(payload),
        original_filename="geico.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        page_count=len(PAGES), state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    await session.flush()
    for number, text in PAGES.items():
        session.add(Page(source_file_id=source_file.id, page_number=number, text=text))

    doc = Document(
        library_id=library.id, source_file_id=source_file.id,
        page_start=1, page_end=2,
    )
    session.add(doc)
    await session.commit()
    return library, source_file, doc


async def _hold(session, doc, *fields) -> None:
    """Claim fields for a person, the way the edit route will."""
    await field_source.record(
        session, doc.id, list(fields), Kind.HUMAN, actor_id=None
    )
    await session.commit()


async def test_a_hand_set_title_survives_reclassification(session, document):
    """The one that matters."""
    _, _, doc = document
    doc.title = "The deed to the house"
    await _hold(session, doc, "title")

    await _classify(session, doc)
    await session.refresh(doc)

    assert doc.title == "The deed to the house", (
        "classification overwrote a title a person set — the silent revert"
    )


async def test_the_fields_nobody_touched_are_still_improved(session, document):
    """Locking the whole document would forfeit AI help on the nine fields
    nobody corrected. Only the claimed field is off limits."""
    _, _, doc = document
    doc.title = "The deed to the house"
    await _hold(session, doc, "title")

    await _classify(session, doc)
    await session.refresh(doc)

    assert doc.title == "The deed to the house"
    assert doc.document_date.isoformat() == "2026-01-01", "an unheld field was skipped"
    assert doc.correspondent_id is not None
    assert doc.document_type_id is not None


async def test_every_editable_field_can_be_held(session, document):
    """A rule that works for `title` and quietly not for `document_date` is
    worse than no rule, because the failure is invisible until it matters."""
    _, _, doc = document
    doc.title = "Mine"
    doc.summary = "Mine too"
    doc.document_date = date(1999, 12, 31)
    correspondent = Correspondent(
        library_id=doc.library_id, name="Set By Hand", slug="set-by-hand"
    )
    kind = DocumentType(
        library_id=doc.library_id, name="Chosen By Hand", slug="chosen-by-hand"
    )
    session.add_all([correspondent, kind])
    await session.flush()
    doc.correspondent_id = correspondent.id
    doc.document_type_id = kind.id
    await _hold(session, doc, *field_source.EDITABLE)

    await _classify(session, doc)
    await session.refresh(doc)

    assert doc.title == "Mine"
    assert doc.summary == "Mine too"
    assert doc.document_date == date(1999, 12, 31)
    assert doc.correspondent_id == correspondent.id
    assert doc.document_type_id == kind.id


async def test_an_ai_written_field_is_recorded_as_ai(session, document):
    """REQ-064 needs three distinguishable sources, so the model has to claim
    what it writes rather than only humans claiming what they set."""
    _, _, doc = document
    await _classify(session, doc)

    sources = await field_source.sources_for(session, doc.id)
    assert sources["title"].source is Kind.AI
    assert sources["title"].set_by is None


async def test_a_released_field_is_writable_again(session, document):
    """Undo has to give the field back, or it stays frozen at a value nobody
    chose — held by a person whose decision has just been reversed."""
    _, _, doc = document
    doc.title = "The deed to the house"
    await _hold(session, doc, "title")
    await field_source.release(session, doc.id, ["title"])
    await session.commit()

    await _classify(session, doc)
    await session.refresh(doc)

    assert doc.title == "GEICO - Declarations - 4417"


async def test_releasing_does_not_delete_the_record(session, document):
    """REQ-090. The row survives its release, so "somebody set this and then
    took it back" stays answerable."""
    _, _, doc = document
    await _hold(session, doc, "title")
    await field_source.release(session, doc.id, ["title"])
    await session.commit()

    row = (
        await session.execute(
            sa.select(FieldSource).where(
                FieldSource.document_id == doc.id, FieldSource.field_name == "title"
            )
        )
    ).scalar_one()
    assert row.released_at is not None, "the claim was deleted rather than released"


async def test_a_document_nobody_edited_classifies_exactly_as_before(session, document):
    """Every document in the archive predates this table. An absent row must
    mean "nobody has claimed this", not "locked"."""
    _, _, doc = document
    await _classify(session, doc)
    await session.refresh(doc)

    assert doc.title == "GEICO - Declarations - 4417"
    assert doc.document_date.isoformat() == "2026-01-01"


# --------------------------------------------------------------------------
# Tags (T-17.6)
# --------------------------------------------------------------------------


async def _remove_tag_by_hand(session, doc, name: str) -> None:
    """What the route will do: revoke the link, attributed to a person."""
    tag_id = (
        await session.execute(
            sa.select(Tag.id).where(
                Tag.name == name, Tag.library_id == doc.library_id
            )
        )
    ).scalar_one()
    event = AuditEvent(
        entity_type="document", entity_id=doc.id, action="edit",
        actor_type=ActorType.HUMAN, actor_id=None,
    )
    session.add(event)
    await session.flush()
    await session.execute(
        sa.update(DocumentTag)
        .where(DocumentTag.document_id == doc.id, DocumentTag.tag_id == tag_id)
        .values(removed_at=datetime.now(UTC), removed_by_event_id=event.id)
    )
    await session.commit()


async def test_a_tag_a_person_removed_is_not_re_added(session, document):
    """The more infuriating version of the silent revert: removing it again
    does nothing, because the next run puts it straight back."""
    _, _, doc = document
    await _classify(session, doc)
    await _remove_tag_by_hand(session, doc, "insurance")

    await _classify(session, doc)

    live = (
        await session.execute(
            sa.select(Tag.name)
            .join(DocumentTag, DocumentTag.tag_id == Tag.id)
            .where(DocumentTag.document_id == doc.id, DocumentTag.removed_at.is_(None))
        )
    ).scalars().all()
    assert "insurance" not in live, "the model re-added a tag a person removed"
    assert "vehicle" in live, "an untouched tag was lost"


async def test_a_tag_the_pipeline_removed_may_come_back(session, document):
    """Only a *person's* removal is binding. An automated one is the pipeline
    changing its mind, which it is allowed to do."""
    _, _, doc = document
    await _classify(session, doc)

    tag_id = (
        await session.execute(
            sa.select(Tag.id).where(
                Tag.name == "insurance", Tag.library_id == doc.library_id
            )
        )
    ).scalar_one()
    event = AuditEvent(
        entity_type="document", entity_id=doc.id, action="classify",
        actor_type=ActorType.AI, actor_id=None,
    )
    session.add(event)
    await session.flush()
    await session.execute(
        sa.update(DocumentTag)
        .where(DocumentTag.document_id == doc.id, DocumentTag.tag_id == tag_id)
        .values(removed_at=datetime.now(UTC), removed_by_event_id=event.id)
    )
    await session.commit()

    await _classify(session, doc)

    live = (
        await session.execute(
            sa.select(Tag.name)
            .join(DocumentTag, DocumentTag.tag_id == Tag.id)
            .where(DocumentTag.document_id == doc.id, DocumentTag.removed_at.is_(None))
        )
    ).scalars().all()
    assert "insurance" in live
