"""The two screens the Scope of Work never had tasks for.

Settings (screen 19) and the archive browser (screen 2) are both in the UX
inventory and neither had a task, so no phase would have built them. Search
answers "where is the thing I know I have"; the archive answers "what is in
here", which had no answer at all — an empty query returned nothing.
"""

import hashlib
import uuid

import pytest
import sqlalchemy as sa

from api import settings_store
from api.db.enums import IngestSource, MembershipRole, ReviewState, SourceFileState, TagSource
from api.db.models import (
    AuditEvent,
    Correspondent,
    Document,
    DocumentTag,
    DocumentType,
    Page,
    Setting,
    SourceFile,
    Tag,
)


@pytest.fixture
async def archive(session, signed_in):
    """A small archive: one scanned bill, one uploaded bundle segment."""
    _, library = await signed_in()
    correspondent = Correspondent(library_id=library.id, name="City Water", slug="city-water")
    doctype = DocumentType(library_id=library.id, name="Utility Bill", slug="utility-bill")
    tag = Tag(library_id=library.id, name="utilities", slug="utilities")
    session.add_all([correspondent, doctype, tag])
    await session.flush()

    made = []
    for index, (name, source, state) in enumerate([
        ("water-bill.png", IngestSource.WATCHED_FOLDER, ReviewState.FILED),
        ("bundle.pdf", IngestSource.WEB_UPLOAD, ReviewState.NEEDS_REVIEW),
    ]):
        sha = hashlib.sha256(f"{name}{uuid.uuid4()}".encode()).hexdigest()
        source_file = SourceFile(
            library_id=library.id, sha256=sha, byte_size=100, original_filename=name,
            ingest_source=source, page_count=3, state=SourceFileState.PROCESSED,
        )
        session.add(source_file)
        await session.flush()
        for page in (1, 2, 3):
            session.add(Page(source_file_id=source_file.id, page_number=page, text=f"page {page}"))
        document = Document(
            library_id=library.id, source_file_id=source_file.id,
            page_start=1, page_end=3 if index == 0 else 1,
            title=f"Document {index}", review_state=state,
            correspondent_id=correspondent.id if index == 0 else None,
            document_type_id=doctype.id if index == 0 else None,
        )
        session.add(document)
        await session.flush()
        if index == 0:
            session.add(
                DocumentTag(document_id=document.id, tag_id=tag.id, source=TagSource.AI)
            )
        made.append(document)
    await session.commit()
    return library, made


# --------------------------------------------------------------------------
# Archive browser
# --------------------------------------------------------------------------


async def test_the_archive_lists_everything_without_a_query(client, archive) -> None:
    """The gap this fills: an empty search showed nothing at all."""
    body = (await client.get("/api/archive")).json()
    assert body["total"] == 2
    assert {e["title"] for e in body["entries"]} == {"Document 0", "Document 1"}


async def test_each_row_says_how_the_document_got_in(client, archive) -> None:
    body = (await client.get("/api/archive")).json()
    sources = {e["title"]: e["ingest_source"] for e in body["entries"]}
    assert sources == {"Document 0": "watched_folder", "Document 1": "web_upload"}


async def test_rows_carry_tags_with_their_provenance(client, archive) -> None:
    body = (await client.get("/api/archive")).json()
    entry = next(e for e in body["entries"] if e["title"] == "Document 0")
    assert [(t["name"], t["source"]) for t in entry["tags"]] == [("utilities", "ai")]


async def test_stats_summarise_the_archive(client, archive) -> None:
    stats = (await client.get("/api/archive")).json()["stats"]
    assert stats["documents"] == 2
    assert stats["files"] == 2
    assert stats["pages"] == 6
    assert stats["needs_review"] == 1


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("q=Document+0", {"Document 0"}),
        ("correspondent=City+Water", {"Document 0"}),
        ("document_type=Utility+Bill", {"Document 0"}),
        ("tag=utilities", {"Document 0"}),
        ("review_state=needs_review", {"Document 1"}),
    ],
)
async def test_filters_narrow_the_list(client, archive, query, expected) -> None:
    body = (await client.get(f"/api/archive?{query}")).json()
    assert {e["title"] for e in body["entries"]} == expected


async def test_the_tree_groups_the_archive(client, archive) -> None:
    body = (await client.get("/api/archive/tree?group_by=correspondent")).json()
    labels = {g["label"]: g["count"] for g in body["groups"]}
    assert labels == {"City Water": 1, "(no correspondent)": 1}


async def test_the_archive_is_library_scoped(client, archive, signed_in) -> None:
    await client.post("/api/auth/logout")
    await signed_in(library_name="Someone Else")
    body = (await client.get("/api/archive")).json()
    assert body["total"] == 0
    assert body["stats"]["documents"] == 0


async def test_the_archive_requires_authentication(client) -> None:
    await client.post("/api/auth/logout")
    assert (await client.get("/api/archive")).status_code == 401


async def test_superseded_segments_are_not_listed(client, session, archive) -> None:
    from datetime import UTC, datetime

    _, documents = archive
    documents[0].superseded_at = datetime.now(UTC)
    await session.commit()

    body = (await client.get("/api/archive")).json()
    assert {e["title"] for e in body["entries"]} == {"Document 1"}


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


async def test_settings_report_no_key_by_default(client, signed_in) -> None:
    await signed_in()
    body = (await client.get("/api/settings")).json()
    assert body["anthropic_key_configured"] is False
    assert body["anthropic_key_hint"] is None


async def test_saving_a_key_never_returns_it(client, signed_in) -> None:
    """A settings page that renders your key into the DOM has leaked it."""
    await signed_in()
    body = (
        await client.put("/api/settings", json={"anthropic_api_key": "sk-ant-secret-value-1234"})
    ).json()

    assert body["anthropic_key_configured"] is True
    assert body["anthropic_key_hint"] == "…1234"
    assert "sk-ant-secret-value-1234" not in str(body)


async def test_the_key_is_encrypted_at_rest(client, session, signed_in) -> None:
    await signed_in()
    await client.put("/api/settings", json={"anthropic_api_key": "sk-ant-secret-value-1234"})

    stored = (
        await session.execute(
            sa.select(Setting).where(Setting.key == settings_store.ANTHROPIC_API_KEY)
        )
    ).scalar_one()
    assert stored.is_secret is True
    assert "sk-ant" not in (stored.value or "")
    # But it round-trips for the code that needs it.
    assert await settings_store.get(session, settings_store.ANTHROPIC_API_KEY) == (
        "sk-ant-secret-value-1234"
    )


async def test_the_audit_records_that_a_secret_changed_not_its_value(
    client, session, signed_in
) -> None:
    await signed_in()
    await client.put("/api/settings", json={"anthropic_api_key": "sk-ant-secret-value-1234"})

    event = (
        await session.execute(
            sa.select(AuditEvent).where(AuditEvent.action == "update_settings")
        )
    ).scalars().first()
    assert event.after == {"changed": ["anthropic_api_key"]}
    assert "sk-ant" not in str(event.after)


async def test_an_empty_key_clears_it(client, signed_in) -> None:
    await signed_in()
    await client.put("/api/settings", json={"anthropic_api_key": "sk-ant-value-9999"})
    body = (await client.put("/api/settings", json={"anthropic_api_key": ""})).json()
    assert body["anthropic_key_configured"] is False


async def test_settings_override_the_environment(client, session, signed_in) -> None:
    """A value set in the UI must win, or the UI looks broken."""
    await signed_in()
    await client.put("/api/settings", json={"model": "claude-sonnet-5"})
    assert await settings_store.get(session, settings_store.BINDERY_MODEL) == "claude-sonnet-5"


async def test_a_reader_cannot_change_settings(client, signed_in) -> None:
    await signed_in(library_name="Read Only", role=MembershipRole.READER)
    response = await client.put("/api/settings", json={"anthropic_api_key": "sk-ant-x"})
    assert response.status_code == 403


async def test_testing_without_a_key_explains_rather_than_errors(client, signed_in) -> None:
    """The archive works without one — say so instead of reporting a failure."""
    await signed_in()
    body = (await client.post("/api/settings/test-ai")).json()
    assert body["ok"] is False
    assert "works without one" in body["detail"]


async def test_only_known_settings_can_be_written(session) -> None:
    with pytest.raises(ValueError, match="not a writable setting"):
        await settings_store.set_(session, "database_url", "postgres://evil", actor_id=None)


# --------------------------------------------------------------------------
# Choosing a model (Opus / Sonnet / Haiku)
# --------------------------------------------------------------------------


async def test_the_available_models_are_offered_with_their_cost(client, signed_in) -> None:
    await signed_in()
    body = (await client.get("/api/settings")).json()

    ids = [choice["id"] for choice in body["available_models"]]
    assert ids == ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"]
    # Price is the reason anyone opens this list, so it travels with the list.
    assert all(choice["input_per_mtok"] > 0 for choice in body["available_models"])
    assert all(choice["blurb"] for choice in body["available_models"])


async def test_choosing_a_cheaper_model_sticks(client, session, signed_in) -> None:
    await signed_in()
    response = await client.put("/api/settings", json={"model": "claude-haiku-4-5-20251001"})
    assert response.status_code == 200, response.text
    assert response.json()["model"] == "claude-haiku-4-5-20251001"

    from api import settings_store

    assert await settings_store.get(session, settings_store.BINDERY_MODEL) == (
        "claude-haiku-4-5-20251001"
    )


async def test_a_model_outside_the_set_is_refused_at_the_form(client, signed_in) -> None:
    """Not accepted and discovered later.

    An unusable model id fails every classification, at the worker, hours
    later — a queue full of dead letters whose cause is a typo on a settings
    page nobody is looking at any more.
    """
    await signed_in()
    response = await client.put("/api/settings", json={"model": "claude-opus-4"})
    assert response.status_code == 422
    assert "not one of the available models" in response.text


def test_spend_is_costed_at_the_model_that_actually_ran() -> None:
    """Switching to Haiku does not make last month cheaper."""
    from api.health_panel import estimate_cost

    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    opus = estimate_cost(usage, "claude-opus-5")
    sonnet = estimate_cost(usage, "claude-sonnet-5")
    haiku = estimate_cost(usage, "claude-haiku-4-5-20251001")

    assert opus > sonnet > haiku
    assert haiku == pytest.approx(6.0)


def test_an_unknown_model_is_costed_pessimistically() -> None:
    """A classification written before the set changed should read as alarming
    rather than reassuring — the tripwire exists to be tripped."""
    from api.health_panel import estimate_cost

    usage = {"input_tokens": 1_000_000}
    assert estimate_cost(usage, "some-retired-model") == estimate_cost(
        usage, "claude-opus-5"
    )
