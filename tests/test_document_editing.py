"""Correcting a document by hand (T-17.2 to T-17.4, T-17.7, T-17.12).

The archive could find any document in ten seconds and could not fix one of
them. These are the properties that make fixing safe: the change is scoped,
audited, undoable, and — the part Phase 17 exists for — it survives.
"""

import uuid
from datetime import date

import pytest
import sqlalchemy as sa

from api import field_source
from api.db.enums import (
    ActorType,
    IngestSource,
    ReviewState,
    SourceFileState,
    TagSource,
)
from api.db.enums import FieldSource as Kind
from api.db.models import (
    AuditEvent,
    Correspondent,
    Document,
    DocumentTag,
    DocumentType,
    SourceFile,
    Tag,
)


@pytest.fixture
async def filed(session, signed_in):
    """A filed document with an AI-written title, as classification leaves it."""
    user, library = await signed_in()
    source = SourceFile(
        library_id=library.id, sha256=uuid.uuid4().hex * 2, byte_size=10,
        original_filename="statement.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1, state=SourceFileState.PROCESSED,
    )
    session.add(source)
    await session.flush()
    document = Document(
        library_id=library.id, source_file_id=source.id, page_start=1, page_end=1,
        title="Northwood Mutal - Statment",  # the model's spelling
        document_date=date(2026, 1, 1),
        review_state=ReviewState.FILED,
    )
    session.add(document)
    await session.flush()
    await field_source.record(
        session, document.id, ["title", "document_date"], Kind.AI
    )
    await session.commit()
    return user, library, document


# --------------------------------------------------------------------------
# The route (REQ-188)
# --------------------------------------------------------------------------


async def test_a_title_can_be_fixed(client, session, filed):
    _, _, document = filed
    response = await client.patch(
        f"/api/documents/{document.id}",
        json={"title": "Northwood Mutual — Statement"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["changed"] == ["title"]

    await session.refresh(document)
    assert document.title == "Northwood Mutual — Statement"


async def test_the_correction_claims_the_field(client, session, filed):
    """Otherwise tonight's classification puts the misspelling back."""
    _, _, document = filed
    await client.patch(
        f"/api/documents/{document.id}", json={"title": "Northwood Mutual — Statement"}
    )

    held = await field_source.held_by_human(session, document.id)
    assert held == {"title"}, "the corrected field was not claimed for the person"

    sources = await field_source.sources_for(session, document.id)
    assert sources["title"].source is Kind.HUMAN
    assert sources["document_date"].source is Kind.AI, "an untouched field was claimed"


async def test_an_absent_field_is_left_alone(client, session, filed):
    """`exclude_unset` is doing real work: a payload that does not mention the
    date must not clear it."""
    _, _, document = filed
    await client.patch(f"/api/documents/{document.id}", json={"title": "Fixed"})

    await session.refresh(document)
    assert document.document_date == date(2026, 1, 1)


async def test_a_field_sent_as_null_is_cleared(client, session, filed):
    """The other half of the same distinction — without it there is no way to
    remove a wrong date, only to replace it with another wrong one."""
    _, _, document = filed
    response = await client.patch(
        f"/api/documents/{document.id}", json={"document_date": None}
    )
    assert response.status_code == 200, response.text

    await session.refresh(document)
    assert document.document_date is None
    assert (await field_source.held_by_human(session, document.id)) == {"document_date"}


async def test_setting_a_field_to_what_it_already_says_changes_nothing(
    client, session, filed
):
    """A no-op must not claim the field. Otherwise opening the form and saving
    without typing anything freezes every value against the model."""
    _, _, document = filed
    response = await client.patch(
        f"/api/documents/{document.id}", json={"title": document.title}
    )
    assert response.json()["changed"] == []
    assert await field_source.held_by_human(session, document.id) == set()


async def test_an_edit_is_audited_as_an_edit(client, session, filed):
    """`undo.UNDOABLE` has known this action since Phase 3."""
    _, _, document = filed
    await client.patch(f"/api/documents/{document.id}", json={"title": "Fixed"})

    event = (
        await session.execute(
            sa.select(AuditEvent).where(
                AuditEvent.entity_id == document.id, AuditEvent.action == "edit"
            )
        )
    ).scalar_one()
    assert event.actor_type is ActorType.HUMAN
    assert event.before == {"title": "Northwood Mutal - Statment"}
    assert event.after == {"title": "Fixed"}


async def test_an_unknown_field_is_refused(client, filed):
    _, _, document = filed
    response = await client.patch(
        f"/api/documents/{document.id}", json={"known_form_id": str(uuid.uuid4())}
    )
    assert response.status_code == 422, "known form is deliberately not editable"


# --------------------------------------------------------------------------
# Taxonomy, by id and by explicit creation (REQ-190, invariant 6)
# --------------------------------------------------------------------------


async def test_a_correspondent_is_linked_by_id(client, session, filed):
    _, library, document = filed
    who = Correspondent(library_id=library.id, name="Northwood Mutual", slug="northwood")
    session.add(who)
    await session.commit()

    response = await client.patch(
        f"/api/documents/{document.id}", json={"correspondent_id": str(who.id)}
    )
    assert response.status_code == 200, response.text
    await session.refresh(document)
    assert document.correspondent_id == who.id


async def test_a_correspondent_from_another_library_is_refused(
    client, session, filed, user_factory
):
    """The access boundary leaking sideways through a field nobody thinks of as
    a permission.

    `user_factory` rather than `signed_in`: signing in as the other person would
    make the PATCH 404 on the document instead, which passes for the wrong
    reason and tests nothing about correspondents.
    """
    _, _, document = filed
    _, elsewhere = await user_factory(email=f"other-{uuid.uuid4().hex[:8]}@example.test")
    theirs = Correspondent(library_id=elsewhere.id, name="Theirs", slug="theirs")
    session.add(theirs)
    await session.commit()

    response = await client.patch(
        f"/api/documents/{document.id}", json={"correspondent_id": str(theirs.id)}
    )
    assert response.status_code == 422
    await session.refresh(document)
    assert document.correspondent_id is None


async def test_a_new_correspondent_is_created_explicitly(client, session, filed):
    """Never by an unmatched name falling through to a create — the caller says
    in as many words that it wants a new one (invariant 6)."""
    _, library, document = filed
    response = await client.patch(
        f"/api/documents/{document.id}",
        json={"create_correspondent": "Fenwick Garage"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["created"]["correspondent"] == "Fenwick Garage"

    await session.refresh(document)
    made = await session.get(Correspondent, document.correspondent_id)
    assert made.name == "Fenwick Garage"
    assert made.library_id == library.id


async def test_a_new_document_type_is_created_explicitly(client, session, filed):
    _, _, document = filed
    response = await client.patch(
        f"/api/documents/{document.id}", json={"create_document_type": "Bank Statement"}
    )
    assert response.status_code == 200, response.text
    await session.refresh(document)
    kind = await session.get(DocumentType, document.document_type_id)
    assert kind.name == "Bank Statement"


# --------------------------------------------------------------------------
# Tags (REQ-189)
# --------------------------------------------------------------------------


async def test_a_tag_can_be_added_and_is_marked_human(client, session, filed):
    _, library, document = filed
    tag = Tag(library_id=library.id, name="banking", slug="banking")
    session.add(tag)
    await session.commit()

    response = await client.patch(
        f"/api/documents/{document.id}", json={"add_tag_ids": [str(tag.id)]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["tags_added"] == ["banking"]

    link = (
        await session.execute(
            sa.select(DocumentTag).where(
                DocumentTag.document_id == document.id, DocumentTag.tag_id == tag.id
            )
        )
    ).scalar_one()
    assert link.source is TagSource.HUMAN
    assert link.removed_at is None


async def test_a_new_tag_can_be_created_while_editing(client, session, filed):
    _, _, document = filed
    response = await client.patch(
        f"/api/documents/{document.id}", json={"create_tags": ["escrow"]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["tags_added"] == ["escrow"]


async def test_removing_a_tag_revokes_the_link_rather_than_deleting_it(
    client, session, filed
):
    """REQ-090, and the mechanism T-17.6 needs: `removed_by_event_id` is how
    classification tells a person's removal from its own."""
    _, library, document = filed
    tag = Tag(library_id=library.id, name="banking", slug="banking")
    session.add(tag)
    await session.flush()
    session.add(
        DocumentTag(document_id=document.id, tag_id=tag.id, source=TagSource.AI)
    )
    await session.commit()

    response = await client.patch(
        f"/api/documents/{document.id}", json={"remove_tag_ids": [str(tag.id)]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["tags_removed"] == ["banking"]

    link = (
        await session.execute(
            sa.select(DocumentTag).where(
                DocumentTag.document_id == document.id, DocumentTag.tag_id == tag.id
            )
        )
    ).scalar_one()
    assert link.removed_at is not None, "the link was deleted rather than revoked"
    assert link.removed_by_event_id is not None, (
        "nothing records that a person removed it, so classification will re-add it"
    )


# --------------------------------------------------------------------------
# The boundary (REQ-099, T-17.12)
# --------------------------------------------------------------------------


async def test_editing_a_document_you_cannot_see_is_a_404(client, session, signed_in):
    """404, never 403: a 403 confirms the document exists, and a probe should
    learn nothing (ADR-005)."""
    _, elsewhere = await signed_in(email=f"a-{uuid.uuid4().hex[:8]}@example.test")
    source = SourceFile(
        library_id=elsewhere.id, sha256=uuid.uuid4().hex * 2, byte_size=1,
        original_filename="theirs.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1, state=SourceFileState.PROCESSED,
    )
    session.add(source)
    await session.flush()
    theirs = Document(
        library_id=elsewhere.id, source_file_id=source.id,
        page_start=1, page_end=1, title="Not yours",
    )
    session.add(theirs)
    await session.commit()

    # Sign in as somebody else entirely.
    await signed_in(email=f"b-{uuid.uuid4().hex[:8]}@example.test")
    response = await client.patch(
        f"/api/documents/{theirs.id}", json={"title": "Mine now"}
    )
    assert response.status_code == 404

    await session.refresh(theirs)
    assert theirs.title == "Not yours"


async def test_a_document_in_a_locked_vault_cannot_be_edited(
    client, session, signed_in, tmp_path, monkeypatch
):
    """Invisible while locked, through the same repository scoping that hides
    it everywhere else (REQ-180)."""
    from api.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    user, library = await signed_in()
    source = SourceFile(
        library_id=library.id, sha256=uuid.uuid4().hex * 2, byte_size=1,
        original_filename="private.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1, state=SourceFileState.PROCESSED,
    )
    session.add(source)
    await session.flush()
    document = Document(
        library_id=library.id, source_file_id=source.id,
        page_start=1, page_end=1, title=None, vaulted_by=user.id,
    )
    session.add(document)
    await session.commit()

    response = await client.patch(
        f"/api/documents/{document.id}", json={"title": "Guessed"}
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Undo (T-17.7, REQ-067)
# --------------------------------------------------------------------------


async def test_an_edit_can_be_undone(client, session, filed):
    """`undo.UNDOABLE` has defined the `edit` action since Phase 3 and nothing
    ever recorded one. This is the route it was waiting for."""
    _, _, document = filed
    await client.patch(f"/api/documents/{document.id}", json={"title": "Fixed"})

    response = await client.post(f"/api/documents/{document.id}/undo")
    assert response.status_code == 200, response.text

    await session.refresh(document)
    assert document.title == "Northwood Mutal - Statment"


async def test_undoing_an_edit_gives_the_field_back(client, session, filed):
    """Otherwise the value stays frozen at something nobody chose — held by a
    person whose decision has just been reversed."""
    _, _, document = filed
    await client.patch(f"/api/documents/{document.id}", json={"title": "Fixed"})
    assert await field_source.held_by_human(session, document.id) == {"title"}

    await client.post(f"/api/documents/{document.id}/undo")

    assert await field_source.held_by_human(session, document.id) == set(), (
        "the field is still claimed, so classification will never write it again"
    )


async def test_undoing_an_edit_puts_back_a_tag_it_removed(client, session, filed):
    """The removal is what stops classification re-applying the tag, so leaving
    it standing would make the undo silently do half its job."""
    _, library, document = filed
    tag = Tag(library_id=library.id, name="banking", slug="banking")
    session.add(tag)
    await session.flush()
    session.add(
        DocumentTag(document_id=document.id, tag_id=tag.id, source=TagSource.AI)
    )
    await session.commit()

    await client.patch(
        f"/api/documents/{document.id}", json={"remove_tag_ids": [str(tag.id)]}
    )
    await client.post(f"/api/documents/{document.id}/undo")

    link = (
        await session.execute(
            sa.select(DocumentTag).where(
                DocumentTag.document_id == document.id, DocumentTag.tag_id == tag.id
            )
        )
    ).scalar_one()
    assert link.removed_at is None, "the tag stayed removed after the undo"


async def test_undoing_an_edit_takes_back_a_tag_it_added(client, session, filed):
    _, library, document = filed
    tag = Tag(library_id=library.id, name="escrow", slug="escrow")
    session.add(tag)
    await session.commit()

    await client.patch(
        f"/api/documents/{document.id}", json={"add_tag_ids": [str(tag.id)]}
    )
    await client.post(f"/api/documents/{document.id}/undo")

    link = (
        await session.execute(
            sa.select(DocumentTag).where(
                DocumentTag.document_id == document.id, DocumentTag.tag_id == tag.id
            )
        )
    ).scalar_one()
    assert link.removed_at is not None, "the added tag survived the undo"


async def test_an_undo_does_not_reach_a_later_edits_tags(client, session, filed):
    """The reason the edit records a manifest instead of undo inferring one
    from timestamps: inference sweeps up what a later edit did."""
    _, library, document = filed
    first = Tag(library_id=library.id, name="first", slug="first")
    second = Tag(library_id=library.id, name="second", slug="second")
    session.add_all([first, second])
    await session.commit()

    await client.patch(
        f"/api/documents/{document.id}", json={"add_tag_ids": [str(first.id)]}
    )
    await client.patch(
        f"/api/documents/{document.id}", json={"add_tag_ids": [str(second.id)]}
    )
    # Undo walks back the most recent edit — the one that added `second`.
    await client.post(f"/api/documents/{document.id}/undo")

    live = (
        await session.execute(
            sa.select(Tag.name)
            .join(DocumentTag, DocumentTag.tag_id == Tag.id)
            .where(
                DocumentTag.document_id == document.id,
                DocumentTag.removed_at.is_(None),
            )
        )
    ).scalars().all()
    assert "first" in live, "the undo reached back into an earlier edit"
    assert "second" not in live
