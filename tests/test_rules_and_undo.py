"""T-3.12 / T-3.13 — rules, and putting automated decisions back.

Two properties that protect trust: a rule cannot act before you have seen what
it would do, and every automated action can be walked back exactly.
"""

import hashlib
import uuid

import pytest
import sqlalchemy as sa

from api.db.enums import (
    ActorType,
    IngestSource,
    JobStage,
    ReviewState,
    Sensitivity,
    SourceFileState,
    TagSource,
)
from api.db.models import (
    AuditEvent,
    Document,
    DocumentTag,
    Page,
    Rule,
    SourceFile,
    Tag,
)
from api.queue import ClaimedJob
from api.storage.blobs import blob_path
from worker.ai import RecordedProvider, set_provider
from worker.stages.classify import run_classify
from worker.stages.rules import run_rules

GEICO_PAGE = (
    "GEICO GENERAL INSURANCE COMPANY\nAUTOMOBILE POLICY DECLARATIONS\n"
    "Policy number 44-1192-4417\nPolicy period: 2026-01-01 to 2026-07-01"
)

RESPONSE = {
    "title": "GEICO - Declarations - 4417",
    "summary": "Automobile policy declarations page.",
    "document_date": "2026-01-01",
    "language": "en",
    "correspondent": {"existing_id": None, "new_name": "GEICO"},
    "document_type": {"existing_id": None, "new_name": "Insurance Declarations"},
    "tags": {"existing_ids": [], "new_names": ["insurance"]},
    "confidence": {"title": 0.9},
    "evidence": [
        {"field": "document_date", "page": 1,
         "snippet": "Policy period: 2026-01-01 to 2026-07-01"}
    ],
}


@pytest.fixture(autouse=True)
def restore_provider():
    yield
    set_provider(None)


@pytest.fixture
async def classified(session, signed_in):
    """A document that has been through the classifier."""
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
    session.add(Page(source_file_id=source_file.id, page_number=1, text=GEICO_PAGE))
    document = Document(
        library_id=library.id, source_file_id=source_file.id, page_start=1, page_end=1
    )
    session.add(document)
    await session.commit()

    set_provider(RecordedProvider(default=RESPONSE))
    await run_classify(
        session,
        ClaimedJob(id=uuid.uuid4(), stage=JobStage.CLASSIFY, source_file_id=None,
                   document_id=document.id, prompt_version=None, attempts=1),
    )
    await session.commit()
    return library, document


def _rules_job(document_id) -> ClaimedJob:
    return ClaimedJob(
        id=uuid.uuid4(), stage=JobStage.RULES, source_file_id=None,
        document_id=document_id, prompt_version=None, attempts=1,
    )


async def _add_rule(session, library, **overrides) -> Rule:
    rule = Rule(
        library_id=library.id,
        name=overrides.get("name", "GEICO is vehicle insurance"),
        enabled=overrides.get("enabled", True),
        conditions=overrides.get(
            "conditions",
            {"all": [{"field": "correspondent", "operator": "equals", "value": "GEICO"}]},
        ),
        actions=overrides.get("actions", {"add_tags": ["vehicle"]}),
    )
    session.add(rule)
    await session.commit()
    return rule


# --------------------------------------------------------------------------
# Rules (REQ-060)
# --------------------------------------------------------------------------


async def test_a_rule_applies_its_tags_with_rule_provenance(session, classified) -> None:
    """The audit must say `rule`, not `ai` — that difference is the point."""
    library, document = classified
    await _add_rule(session, library)

    await run_rules(session, _rules_job(document.id))
    await session.commit()

    rows = (
        await session.execute(
            sa.select(Tag.name, DocumentTag.source)
            .join(DocumentTag, DocumentTag.tag_id == Tag.id)
            .where(DocumentTag.document_id == document.id)
        )
    ).all()
    by_name = dict(rows)
    assert by_name["vehicle"] is TagSource.RULE
    assert by_name["insurance"] is TagSource.AI

    event = (
        await session.execute(
            sa.select(AuditEvent).where(
                AuditEvent.entity_id == document.id, AuditEvent.action == "rule_applied"
            )
        )
    ).scalar_one()
    assert event.actor_type is ActorType.RULE
    assert event.rule_id is not None


async def test_a_rule_that_fires_is_decisive_at_the_gate(session, classified) -> None:
    """You wrote it, so it files."""
    library, document = classified
    await _add_rule(session, library)

    await run_rules(session, _rules_job(document.id))
    await session.commit()
    await session.refresh(document)

    assert document.review_state is ReviewState.FILED


async def test_without_a_matching_rule_the_document_waits_for_review(
    session, classified
) -> None:
    """Invented correspondent, invented tags: nothing structural to stand on."""
    _, document = classified
    await run_rules(session, _rules_job(document.id))
    await session.commit()
    await session.refresh(document)

    assert document.review_state is ReviewState.NEEDS_REVIEW


async def test_a_disabled_rule_does_nothing(session, classified) -> None:
    library, document = classified
    await _add_rule(session, library, enabled=False)

    await run_rules(session, _rules_job(document.id))
    await session.commit()
    await session.refresh(document)

    assert document.review_state is ReviewState.NEEDS_REVIEW


async def test_a_rule_can_set_sensitivity(session, classified) -> None:
    library, document = classified
    await _add_rule(
        session, library,
        conditions={"all": [{"field": "text", "operator": "contains", "value": "policy"}]},
        actions={"set_sensitivity": "sensitive"},
    )
    await run_rules(session, _rules_job(document.id))
    await session.commit()
    await session.refresh(document)

    assert document.sensitivity is Sensitivity.SENSITIVE


async def test_all_conditions_must_hold(session, classified) -> None:
    library, document = classified
    await _add_rule(
        session, library,
        conditions={"all": [
            {"field": "correspondent", "operator": "equals", "value": "GEICO"},
            {"field": "correspondent", "operator": "equals", "value": "Progressive"},
        ]},
    )
    await run_rules(session, _rules_job(document.id))
    await session.commit()
    await session.refresh(document)
    assert document.review_state is ReviewState.NEEDS_REVIEW


async def test_a_malformed_rule_is_refused_at_save_time(session) -> None:
    """Surfaced when it is written, never when it runs."""
    from api.rules import RuleError, validate

    with pytest.raises(RuleError, match="at least one condition"):
        validate({}, {"add_tags": ["x"]})
    with pytest.raises(RuleError, match="unknown condition field"):
        validate({"all": [{"field": "nonsense", "value": "x"}]}, {"add_tags": ["x"]})
    with pytest.raises(RuleError, match="at least one action"):
        validate({"all": [{"field": "title", "value": "x"}]}, {})


# --------------------------------------------------------------------------
# Undo (REQ-067)
# --------------------------------------------------------------------------


async def test_undo_returns_a_classified_document_to_review(client, session, classified) -> None:
    _, document = classified
    await run_rules(session, _rules_job(document.id))
    await session.commit()

    response = await client.post(f"/api/documents/{document.id}/undo")

    assert response.status_code == 200
    assert response.json()["review_state"] == ReviewState.NEEDS_REVIEW.value


async def test_undo_restores_the_exact_prior_state(client, session, classified) -> None:
    """Verified by the audit diff, not by inspection."""
    _, document = classified
    await client.post(f"/api/documents/{document.id}/accept")

    response = await client.post(f"/api/documents/{document.id}/undo")
    assert response.status_code == 200

    event = (
        await session.execute(
            sa.select(AuditEvent)
            .where(AuditEvent.entity_id == document.id, AuditEvent.action == "undo")
            .order_by(AuditEvent.sequence.desc())
            .limit(1)
        )
    ).scalar_one()
    assert event.after["undid"] == "file"
    assert event.after["restored"]["review_state"] == ReviewState.PENDING_CLASSIFICATION.value


async def test_undo_is_itself_audited_and_reversible(client, session, classified) -> None:
    """Nothing is deleted, so the undo can be undone in turn."""
    _, document = classified
    await client.post(f"/api/documents/{document.id}/accept")
    await client.post(f"/api/documents/{document.id}/undo")

    actions = (
        await session.execute(
            sa.select(AuditEvent.action)
            .where(AuditEvent.entity_id == document.id)
            .order_by(AuditEvent.sequence)
        )
    ).scalars().all()
    assert actions == ["classify", "file", "undo"]


async def test_undo_with_nothing_to_undo_is_a_conflict(client, session, signed_in) -> None:
    _, library = await signed_in()
    payload = b"%PDF-" + uuid.uuid4().bytes
    sha = hashlib.sha256(payload).hexdigest()
    source_file = SourceFile(
        library_id=library.id, sha256=sha, byte_size=5,
        ingest_source=IngestSource.WEB_UPLOAD, page_count=1,
    )
    session.add(source_file)
    await session.flush()
    document = Document(
        library_id=library.id, source_file_id=source_file.id, page_start=1, page_end=1
    )
    session.add(document)
    await session.commit()

    assert (await client.post(f"/api/documents/{document.id}/undo")).status_code == 409


async def test_undo_of_another_library_s_document_is_a_404(
    client, session, classified, signed_in
) -> None:
    _, document = classified
    await client.post("/api/auth/logout")
    await signed_in(library_name="Someone Else")
    assert (await client.post(f"/api/documents/{document.id}/undo")).status_code == 404


# --------------------------------------------------------------------------
# Review queue and why-panel (REQ-059, REQ-063, REQ-064)
# --------------------------------------------------------------------------


async def test_a_low_signal_document_lands_in_the_review_queue(
    client, session, classified
) -> None:
    _, document = classified
    await run_rules(session, _rules_job(document.id))
    await session.commit()

    body = (await client.get("/api/review")).json()
    assert body["total"] >= 1
    assert str(document.id) in {row["id"] for row in body["documents"]}


async def test_the_why_panel_shows_the_source_sentence_and_page(
    client, session, classified
) -> None:
    """REQ-063 — a direct read of stored provenance."""
    _, document = classified
    body = (await client.get(f"/api/documents/{document.id}/why")).json()

    by_field = {row["field_name"]: row for row in body["provenance"]}
    assert by_field["document_date"]["snippet"] == (
        "Policy period: 2026-01-01 to 2026-07-01"
    )
    assert by_field["document_date"]["page_number"] == 1


async def test_the_why_panel_distinguishes_ai_rule_and_human_values(
    client, session, classified
) -> None:
    """REQ-064 — the UI cannot make the distinction the API does not carry."""
    library, document = classified
    await _add_rule(session, library)
    await run_rules(session, _rules_job(document.id))
    await session.commit()

    body = (await client.get(f"/api/documents/{document.id}/why")).json()
    sources = {tag["name"]: tag["source"] for tag in body["tags"]}
    assert sources == {"insurance": "ai", "vehicle": "rule"}


async def test_the_why_panel_shows_confidence_and_the_gate_s_reasoning(
    client, session, classified
) -> None:
    """REQ-065 — confidence is displayed, and separately from what decided."""
    _, document = classified
    body = (await client.get(f"/api/documents/{document.id}/why")).json()

    assert body["classification"]["confidence"]["title"] == 0.9
    assert body["classification"]["gate_decision"] == "needs_review"
    assert body["classification"]["structural_signals"]["model_confidence"]["title"] == 0.9


async def test_the_review_queue_is_library_scoped(client, session, classified, signed_in) -> None:
    _, document = classified
    await run_rules(session, _rules_job(document.id))
    await session.commit()

    await client.post("/api/auth/logout")
    await signed_in(library_name="Someone Else")
    assert (await client.get("/api/review")).json()["total"] == 0


async def test_undo_withdraws_the_tags_the_classifier_applied(
    client, session, classified
) -> None:
    """Superseded, not deleted — the record that they were applied survives."""
    from api.db.models import live_tag_links

    _, document = classified
    before = (
        await session.execute(
            sa.select(Tag.name)
            .join(DocumentTag, DocumentTag.tag_id == Tag.id)
            .where(DocumentTag.document_id == document.id, live_tag_links())
        )
    ).scalars().all()
    assert before == ["insurance"]

    assert (await client.post(f"/api/documents/{document.id}/undo")).status_code == 200

    live = (
        await session.execute(
            sa.select(Tag.name)
            .join(DocumentTag, DocumentTag.tag_id == Tag.id)
            .where(DocumentTag.document_id == document.id, live_tag_links())
        )
    ).scalars().all()
    assert live == []

    # The link row is still there, carrying the event that withdrew it.
    row = (
        await session.execute(
            sa.select(DocumentTag).where(DocumentTag.document_id == document.id)
        )
    ).scalar_one()
    assert row.removed_at is not None
    assert row.removed_by_event_id is not None


async def test_repeated_undo_walks_back_through_the_chain(client, session, classified) -> None:
    """Each press reverses the next decision down, not the same one again."""
    library, document = classified
    await _add_rule(session, library)
    await run_rules(session, _rules_job(document.id))
    await session.commit()

    undone: list[str] = []
    for _ in range(3):
        response = await client.post(f"/api/documents/{document.id}/undo")
        assert response.status_code == 200
        event = (
            await session.execute(
                sa.select(AuditEvent)
                .where(AuditEvent.entity_id == document.id, AuditEvent.action == "undo")
                .order_by(AuditEvent.sequence.desc())
                .limit(1)
            )
        ).scalar_one()
        undone.append(event.after["undid"])

    assert undone == ["file", "rule_applied", "classify"]
    # And then there is nothing left to walk back.
    assert (await client.post(f"/api/documents/{document.id}/undo")).status_code == 409


async def test_undo_does_not_withdraw_a_rule_s_tags(client, session, classified) -> None:
    """A tag a rule set is not the classifier's to take back."""
    from api.db.models import live_tag_links

    library, document = classified
    await _add_rule(session, library)
    await run_rules(session, _rules_job(document.id))
    await session.commit()

    # Walk all the way back past the classification.
    for _ in range(3):
        await client.post(f"/api/documents/{document.id}/undo")

    live = (
        await session.execute(
            sa.select(Tag.name)
            .join(DocumentTag, DocumentTag.tag_id == Tag.id)
            .where(DocumentTag.document_id == document.id, live_tag_links())
        )
    ).scalars().all()
    assert live == ["vehicle"]
