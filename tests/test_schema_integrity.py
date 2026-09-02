"""The schema the models describe is the schema Alembic actually built.

Two classes of silent divergence live here.

The first is enum drift. `TagSource` grew a fourth member in Phase 18 and no
`ALTER TYPE` followed it, so any write of `TagSource.FILE` would have failed at
the database with *invalid input value for enum tag_source* — in the worker,
hours later, as a dead letter with an opaque error. Nothing would have caught
it: `conftest` builds the schema purely from the migrations, so `Base.metadata`
is never compared against what Alembic produced.

The second is timestamps that lie. `document.updated_at` was declared NOT NULL
and written by nothing, so every document reported `updated_at == created_at`
forever. An absent timestamp is honest; a stale one gets trusted.
"""

import hashlib
import uuid

import sqlalchemy as sa

from api.db.base import Base
from api.db.enums import (
    ImportState,
    IngestSource,
    ReviewState,
    SourceFileState,
    TagSource,
)
from api.db.models import Document, DocumentTag, ImportSession, Page, SourceFile, Tag

PAGE_COUNT = 3


async def _bundle(session, library) -> SourceFile:
    payload = b"%PDF-1.7\n" + uuid.uuid4().bytes * 8 + b"\n%%EOF\n"
    source_file = SourceFile(
        library_id=library.id,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
        original_filename="Bundle.pdf",
        ingest_source=IngestSource.WATCHED_FOLDER,
        page_count=PAGE_COUNT,
        state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    await session.flush()
    for number in range(1, PAGE_COUNT + 1):
        session.add(Page(source_file_id=source_file.id, page_number=number, text="body"))
    await session.commit()
    return source_file


# ---------------------------------------------------------------------------
# Enum drift (CR-070)
# ---------------------------------------------------------------------------


async def test_every_python_enum_matches_the_postgres_type_exactly(session) -> None:
    """The guard that would have caught `tag_source` losing `file`.

    Members are compared as sets rather than sequences: `ALTER TYPE ... ADD
    VALUE` appends, and the sort order of a native enum is a display concern,
    not a correctness one.
    """
    rows = await session.execute(
        sa.text(
            """
            SELECT t.typname, e.enumlabel
              FROM pg_type t
              JOIN pg_enum e ON e.enumtypid = t.oid
            """
        )
    )
    in_database: dict[str, set[str]] = {}
    for typname, label in rows:
        in_database.setdefault(typname, set()).add(label)

    declared: dict[str, set[str]] = {}
    for table in Base.metadata.tables.values():
        for column in table.columns:
            enum_type = getattr(column.type, "enums", None)
            name = getattr(column.type, "name", None)
            if enum_type is None or name is None or not isinstance(column.type, sa.Enum):
                continue
            declared.setdefault(name, set()).update(enum_type)

    assert declared, "the models declare at least one native enum"
    missing = {
        name: sorted(members - in_database.get(name, set()))
        for name, members in declared.items()
        if members - in_database.get(name, set())
    }
    assert not missing, (
        "Python enum members with no Postgres value — every write of one of "
        f"these fails at the database: {missing}. Add an "
        "`ALTER TYPE … ADD VALUE` migration."
    )


async def test_a_tag_link_can_record_that_the_file_itself_said_so(
    session, signed_in
) -> None:
    """`TagSource.FILE` — a capture date read from EXIF — reaches the database.

    Before migration 0028 this raised *invalid input value for enum
    tag_source: "file"*.
    """
    _, library = await signed_in()
    source_file = await _bundle(session, library)
    document = Document(
        library_id=library.id, source_file_id=source_file.id, page_start=1, page_end=3
    )
    tag = Tag(library_id=library.id, name="Taken 2019", slug=f"taken-{uuid.uuid4().hex[:8]}")
    session.add_all([document, tag])
    await session.flush()
    session.add(
        DocumentTag(document_id=document.id, tag_id=tag.id, source=TagSource.FILE)
    )
    await session.commit()

    stored = await session.scalar(
        sa.select(DocumentTag.source).where(DocumentTag.document_id == document.id)
    )
    assert stored == TagSource.FILE


# ---------------------------------------------------------------------------
# updated_at (CR-071)
# ---------------------------------------------------------------------------


async def test_editing_a_document_moves_its_updated_at(session, signed_in) -> None:
    _, library = await signed_in()
    source_file = await _bundle(session, library)
    document = Document(
        library_id=library.id, source_file_id=source_file.id, page_start=1, page_end=3
    )
    session.add(document)
    # Committed separately: `now()` is the transaction clock, so an insert and
    # an update in one transaction share a timestamp by definition.
    await session.commit()
    await session.refresh(document)
    created = document.created_at
    assert document.updated_at == created

    document.title = "Deed of Trust"
    await session.commit()
    await session.refresh(document)

    assert document.updated_at > created, (
        "a corrected document still reports the moment it was created"
    )


async def test_a_bulk_update_moves_updated_at_too(session, signed_in) -> None:
    """The half `onupdate=` would have missed.

    `api/segments.py` and `api/bulk.py` change documents with
    `sa.update(Document)` and never load an object, so a client-side default
    would leave exactly the rows a bulk re-file touched reporting stale.
    """
    _, library = await signed_in()
    source_file = await _bundle(session, library)
    document = Document(
        library_id=library.id, source_file_id=source_file.id, page_start=1, page_end=3
    )
    session.add(document)
    await session.commit()
    await session.refresh(document)
    before = document.updated_at

    await session.execute(
        sa.update(Document)
        .where(Document.id == document.id)
        .values(review_state=ReviewState.FILED.value)
    )
    await session.commit()
    await session.refresh(document)

    assert document.updated_at > before


async def test_an_update_that_changes_nothing_is_not_a_change(session, signed_in) -> None:
    """The trigger's `WHEN (OLD.* IS DISTINCT FROM NEW.*)` clause.

    Rewriting a row with the values it already had is not an edit, and recording
    it as one would make `updated_at` noisy in the other direction.
    """
    _, library = await signed_in()
    source_file = await _bundle(session, library)
    document = Document(
        library_id=library.id,
        source_file_id=source_file.id,
        page_start=1,
        page_end=3,
        title="Unchanged",
    )
    session.add(document)
    await session.commit()
    await session.refresh(document)
    before = document.updated_at

    await session.execute(
        sa.update(Document).where(Document.id == document.id).values(title="Unchanged")
    )
    await session.commit()
    await session.refresh(document)

    assert document.updated_at == before


async def test_an_import_session_records_when_it_last_moved(session, signed_in) -> None:
    _, library = await signed_in()
    import_session = ImportSession(library_id=library.id, root_path="/data/backlog")
    session.add(import_session)
    await session.commit()
    await session.refresh(import_session)
    before = import_session.updated_at

    import_session.state = ImportState.DRY_RUN
    await session.commit()
    await session.refresh(import_session)

    assert import_session.updated_at > before
