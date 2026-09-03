"""Undo has to restore what the action replaced — no more and no less.

Two failures of the same shape, at opposite ends of the trust surface the
governing principle rests on: "one click to reverse". One undo restored *less*
than the action changed (a classification's fields stayed on the document), and
one restored *more* than it created (a bulk tag-add revoked a tag the operation
had deliberately left alone, because it matched by name).
"""

import hashlib
import uuid

import pytest
import sqlalchemy as sa

from api import undo as undo_module
from api.db.enums import IngestSource, JobStage, SourceFileState, TagSource
from api.db.models import Document, DocumentTag, Page, SourceFile, Tag
from api.queue import ClaimedJob
from api.storage.blobs import blob_path
from worker.ai import RecordedProvider, set_provider
from worker.stages.classify import run_classify

FIRST = {
    "title": "GEICO - Declarations - 4417",
    "summary": "Automobile policy declarations page.",
    "document_date": "2026-01-01",
    "language": "en",
    "correspondent": {"existing_id": None, "new_name": "GEICO"},
    "document_type": {"existing_id": None, "new_name": "Insurance Declarations"},
    "tags": {"existing_ids": [], "new_names": ["insurance"]},
    "confidence": {"title": 0.9},
    "evidence": [
        {"field": "document_date", "page": 1, "snippet": "Policy period: 2026-01-01"}
    ],
}

# The same document read again under a new prompt, deciding differently. This is
# the ordinary case for `make enqueue-stage stage=classify`, and it is the one
# where undo has something to give back.
SECOND = {
    **FIRST,
    "title": "Untitled scan",
    "summary": "A scanned page.",
    "document_date": "2019-05-05",
}


@pytest.fixture(autouse=True)
def restore_provider():
    yield
    set_provider(None)


@pytest.fixture
async def document(session, signed_in):
    _, library = await signed_in()
    payload = b"%PDF-" + uuid.uuid4().bytes
    sha = hashlib.sha256(payload).hexdigest()
    path = blob_path(sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)

    source_file = SourceFile(
        library_id=library.id, sha256=sha, byte_size=len(payload),
        original_filename="geico.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1, state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    await session.flush()
    session.add(
        Page(
            source_file_id=source_file.id, page_number=1,
            text="GEICO GENERAL INSURANCE COMPANY\nPolicy period: 2026-01-01",
        )
    )
    document = Document(
        library_id=library.id, source_file_id=source_file.id, page_start=1, page_end=1
    )
    session.add(document)
    await session.commit()
    return library, document


async def _classify(session, document, response) -> None:
    set_provider(RecordedProvider(default=response))
    await run_classify(
        session,
        ClaimedJob(id=uuid.uuid4(), stage=JobStage.CLASSIFY, source_file_id=None,
                   document_id=document.id, prompt_version=None, attempts=1),
    )
    await session.commit()


async def test_undoing_a_classification_gives_the_previous_values_back(
    client, session, document
) -> None:
    """Classify twice, undo once: the first run's title and date come back.

    The classify audit row carried only `after`, so `undo_event` — which
    restores from `before` and skips every name it does not find there —
    restored nothing at all. The tags were withdrawn and the document went back
    to the review queue, which made it *look* undone, while the second run's
    title, summary, date, correspondent and type stayed exactly where they were.
    """
    _library, document = document
    await _classify(session, document, FIRST)
    await session.refresh(document)
    assert document.title == "GEICO - Declarations - 4417"

    await _classify(session, document, SECOND)
    await session.refresh(document)
    assert document.title == "Untitled scan", "the second run did not take"

    assert (await client.post(f"/api/documents/{document.id}/undo")).status_code == 200

    await session.commit()
    await session.refresh(document)
    assert document.title == "GEICO - Declarations - 4417"
    assert document.summary == "Automobile policy declarations page."
    assert document.document_date.isoformat() == "2026-01-01"


async def test_the_undo_row_says_which_fields_it_put_back(session, document) -> None:
    """The Why panel reads `restored`; it has to name more than review_state."""
    from api.db.models import AuditEvent

    _library, document = document
    await _classify(session, document, FIRST)
    await _classify(session, document, SECOND)

    event = await undo_module.latest_undoable(session, document.id)
    assert event is not None and event.action == "classify"
    await undo_module.undo_event(session, event, actor_id=None)
    await session.commit()

    undo_row = (
        await session.execute(
            sa.select(AuditEvent)
            .where(AuditEvent.entity_id == document.id, AuditEvent.action == "undo")
            .order_by(AuditEvent.sequence.desc())
            .limit(1)
        )
    ).scalar_one()
    assert undo_row.after["restored"]["title"] == "GEICO - Declarations - 4417"


async def test_a_first_classification_still_undoes_to_an_empty_document(
    session, document
) -> None:
    """Nothing there before means nothing there after — not the model's answer."""
    _library, document = document
    await _classify(session, document, FIRST)

    event = await undo_module.latest_undoable(session, document.id)
    await undo_module.undo_event(session, event, actor_id=None)
    await session.commit()
    await session.refresh(document)

    assert document.title is None
    assert document.document_date is None


# --------------------------------------------------------------------------
# Bulk undo (REQ-087)
# --------------------------------------------------------------------------


@pytest.fixture
async def documents_one_already_tagged(session, signed_in):
    """Twenty documents. The first was tagged "Taxes" by hand, long ago."""
    _, library = await signed_in()
    source_file = SourceFile(
        library_id=library.id, sha256=uuid.uuid4().hex * 2, byte_size=10,
        ingest_source=IngestSource.BULK_IMPORT, page_count=20,
        state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    await session.flush()
    documents = [
        Document(library_id=library.id, source_file_id=source_file.id,
                 page_start=n, page_end=n, title=f"Doc {n}")
        for n in range(1, 21)
    ]
    session.add_all(documents)
    tag = Tag(library_id=library.id, name="Taxes", slug="taxes")
    session.add(tag)
    await session.flush()
    session.add(
        DocumentTag(document_id=documents[0].id, tag_id=tag.id, source=TagSource.HUMAN)
    )
    await session.commit()
    return library, documents, tag


async def _live_tags(session, document_id) -> list[str]:
    return list(
        (
            await session.execute(
                sa.select(Tag.name)
                .join(DocumentTag, DocumentTag.tag_id == Tag.id)
                .where(
                    DocumentTag.document_id == document_id,
                    DocumentTag.removed_at.is_(None),
                )
            )
        ).scalars().all()
    )


async def test_undoing_a_bulk_add_leaves_a_tag_it_did_not_add(
    client, session, documents_one_already_tagged
) -> None:
    """The year-old "Taxes" on document one survives the undo.

    `apply` skips a document that already carries the tag, but the undo matched
    on the tag *name* and human source, so it revoked a link the operation never
    created — a filing decision from last year, gone, with a second undo refused
    and no way back but re-tagging by hand.
    """
    _library, documents, _tag = documents_one_already_tagged
    applied = (await client.post("/api/bulk/apply", json={
        "document_ids": [str(d.id) for d in documents],
        "actions": {"add_tags": ["Taxes"]},
    })).json()
    await session.commit()

    assert await _live_tags(session, documents[0].id) == ["Taxes"]
    assert await _live_tags(session, documents[1].id) == ["Taxes"]

    assert (
        await client.post(f"/api/bulk/{applied['operation_id']}/undo")
    ).status_code == 200
    await session.commit()

    assert await _live_tags(session, documents[0].id) == ["Taxes"], (
        "the undo revoked a tag the bulk edit did not apply"
    )
    assert await _live_tags(session, documents[1].id) == [], (
        "the undo left behind a tag the bulk edit did apply"
    )


async def test_a_bulk_add_can_follow_its_own_undo(
    client, session, documents_one_already_tagged
) -> None:
    """`(document_id, tag_id)` is the primary key, so the second add has to
    revive the revoked link rather than insert over it."""
    _library, documents, _tag = documents_one_already_tagged
    ids = [str(d.id) for d in documents[1:4]]

    first = (await client.post("/api/bulk/apply", json={
        "document_ids": ids, "actions": {"add_tags": ["Taxes"]},
    })).json()
    assert (await client.post(f"/api/bulk/{first['operation_id']}/undo")).status_code == 200

    again = await client.post("/api/bulk/apply", json={
        "document_ids": ids, "actions": {"add_tags": ["Taxes"]},
    })
    assert again.status_code == 200, again.text

    await session.commit()
    assert await _live_tags(session, documents[1].id) == ["Taxes"]
