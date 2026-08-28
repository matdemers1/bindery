"""T-3.1 to T-3.8 — classification end to end, without a live API call.

> AI calls are never live in the default test run.

Everything here runs against `RecordedProvider`, which validates its fixtures
through the same strict schema a real response goes through — so a fixture that
would not survive the live contract fails here rather than passing quietly.
"""

import hashlib
import uuid

import pytest
import sqlalchemy as sa

from api.db.enums import (
    ActorType,
    IngestSource,
    JobStage,
    LibraryKind,
    ReviewState,
    SourceFileState,
    TagSource,
)
from api.db.models import (
    AuditEvent,
    Classification,
    Correspondent,
    Document,
    DocumentTag,
    DocumentType,
    FieldProvenance,
    Job,
    KnownForm,
    Library,
    Page,
    SourceFile,
    Tag,
)
from api.queue import ClaimedJob
from api.storage.blobs import blob_path
from worker.ai import RecordedProvider, UnavailableProvider, set_provider
from worker.ai.provider import ProviderUnavailableError
from worker.stages.classify import run_classify
from worker.stages.embed import run_embed

PAGES = {
    1: "GEICO GENERAL INSURANCE COMPANY\nAUTOMOBILE POLICY DECLARATIONS\n"
       "Named insured: M DEMERS\nPolicy number 44-1192-4417\n"
       "Policy period: 2026-01-01 to 2026-07-01",
    2: "Coverage limits and premium summary. Effective date: 2026-01-01",
}

RESPONSE = {
    "title": "GEICO - Declarations - 4417",
    "summary": "Automobile policy declarations page for the 2026 policy period.",
    "document_date": "2026-01-01",
    "language": "en",
    "correspondent": {"existing_id": None, "new_name": "GEICO"},
    "document_type": {"existing_id": None, "new_name": "Insurance Declarations"},
    "tags": {"existing_ids": [], "new_names": ["insurance", "vehicle"]},
    "confidence": {"title": 0.94, "document_date": 0.99, "correspondent": 0.97},
    "evidence": [
        {"field": "document_date", "page": 1,
         "snippet": "Policy period: 2026-01-01 to 2026-07-01"},
        {"field": "correspondent", "page": 1, "snippet": "GEICO GENERAL INSURANCE COMPANY"},
    ],
}


@pytest.fixture(autouse=True)
def restore_provider():
    yield
    set_provider(None)


@pytest.fixture
async def document(session, signed_in):
    """A two-page declarations page, segmented and ready to classify."""
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


def _job(document_id) -> ClaimedJob:
    return ClaimedJob(
        id=uuid.uuid4(), stage=JobStage.CLASSIFY, source_file_id=None,
        document_id=document_id, prompt_version=None, attempts=1,
    )


async def _classify(session, doc, **provider_kwargs) -> RecordedProvider:
    provider = RecordedProvider(default=RESPONSE, **provider_kwargs)
    set_provider(provider)
    await run_classify(session, _job(doc.id))
    await session.commit()
    return provider


# --------------------------------------------------------------------------
# The response contract (REQ-045, REQ-046)
# --------------------------------------------------------------------------


async def test_classification_writes_title_date_and_taxonomy(session, document) -> None:
    _, _, doc = document
    await _classify(session, doc)
    await session.refresh(doc)

    assert doc.title == "GEICO - Declarations - 4417"
    assert doc.document_date.isoformat() == "2026-01-01"

    correspondent = await session.get(Correspondent, doc.correspondent_id)
    assert correspondent.name == "GEICO"
    document_type = await session.get(DocumentType, doc.document_type_id)
    assert document_type.name == "Insurance Declarations"


async def test_tags_are_applied_with_ai_provenance(session, document) -> None:
    """REQ-078 — provenance on the link row, so the why-panel can attribute it."""
    _, _, doc = document
    await _classify(session, doc)

    rows = (
        await session.execute(
            sa.select(Tag.name, DocumentTag.source)
            .join(DocumentTag, DocumentTag.tag_id == Tag.id)
            .where(DocumentTag.document_id == doc.id)
        )
    ).all()
    assert {name for name, _ in rows} == {"insurance", "vehicle"}
    assert {source for _, source in rows} == {TagSource.AI}


async def test_an_existing_id_resolves_by_id_and_nothing_else(session, document) -> None:
    """REQ-046. The reused entry must not pass through normalisation."""
    library, _, doc = document
    tag = Tag(library_id=library.id, name="Insurance", slug="insurance")
    correspondent = Correspondent(library_id=library.id, name="GEICO", slug="geico")
    session.add_all([tag, correspondent])
    await session.commit()

    provider = RecordedProvider(default={
        **RESPONSE,
        "correspondent": {"existing_id": str(correspondent.id), "new_name": None},
        "tags": {"existing_ids": [str(tag.id)], "new_names": []},
    })
    set_provider(provider)
    await run_classify(session, _job(doc.id))
    await session.commit()
    await session.refresh(doc)

    assert doc.correspondent_id == correspondent.id
    linked = (
        await session.execute(
            sa.select(DocumentTag.tag_id).where(DocumentTag.document_id == doc.id)
        )
    ).scalars().all()
    assert linked == [tag.id]
    # Reuse means exactly one entry, not a near-duplicate beside it.
    assert (
        await session.execute(sa.select(sa.func.count()).select_from(Tag)
                              .where(Tag.library_id == library.id))
    ).scalar_one() == 1


async def test_an_id_from_another_library_is_refused(session, document, signed_in) -> None:
    """REQ-048 — returned ids are re-validated, not trusted."""
    _, _, doc = document
    elsewhere = Library(name="Not Yours", kind=LibraryKind.PERSONAL)
    session.add(elsewhere)
    await session.flush()
    stranger_tag = Tag(library_id=elsewhere.id, name="Secret", slug="secret")
    session.add(stranger_tag)
    await session.commit()

    provider = RecordedProvider(default={
        **RESPONSE,
        "tags": {"existing_ids": [str(stranger_tag.id)], "new_names": []},
    })
    set_provider(provider)
    await run_classify(session, _job(doc.id))
    await session.commit()

    linked = (
        await session.execute(
            sa.select(DocumentTag.tag_id).where(DocumentTag.document_id == doc.id)
        )
    ).scalars().all()
    assert linked == []

    classification = (
        await session.execute(
            sa.select(Classification).where(Classification.document_id == doc.id)
        )
    ).scalar_one()
    # And it is recorded as a signal, which blocks unattended filing.
    assert classification.structural_signals["rejected_id_count"] == 1
    assert classification.gate_decision == "needs_review"


async def test_a_malformed_response_fails_loudly(session, document) -> None:
    """REQ-045 — never silently accepted, never partially applied."""
    _, _, doc = document
    # Held across the rollback below, which expires every loaded instance.
    document_id = doc.id
    set_provider(RecordedProvider(default={"title": "missing everything else"}))

    with pytest.raises(Exception) as caught:
        await run_classify(session, _job(document_id))
    message = str(caught.value).lower()
    assert "validation" in message or "field required" in message

    await session.rollback()
    fresh = await session.get(Document, document_id)
    assert fresh.title is None


async def test_an_unusable_date_is_dropped_rather_than_stored(session, document) -> None:
    """A wrong date nobody will check is worse than no date."""
    _, _, doc = document
    provider = RecordedProvider(default={**RESPONSE, "document_date": "not-a-date"})
    set_provider(provider)
    await run_classify(session, _job(doc.id))
    await session.commit()
    await session.refresh(doc)

    assert doc.document_date is None
    assert doc.title == "GEICO - Declarations - 4417"


# --------------------------------------------------------------------------
# Provenance (REQ-049, REQ-050)
# --------------------------------------------------------------------------


async def test_every_ai_field_has_provenance_with_page_and_snippet(session, document) -> None:
    _, _, doc = document
    await _classify(session, doc)

    classification = (
        await session.execute(
            sa.select(Classification).where(Classification.document_id == doc.id)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            sa.select(FieldProvenance).where(
                FieldProvenance.classification_id == classification.id
            )
        )
    ).scalars().all()

    by_field = {row.field_name: row for row in rows}
    assert set(by_field) == {"document_date", "correspondent"}
    assert by_field["document_date"].snippet.startswith("Policy period")
    assert by_field["document_date"].page_number == 1
    assert by_field["document_date"].confidence == pytest.approx(0.99)


async def test_provenance_pages_are_absolute_in_the_source_file(session, document) -> None:
    """The model numbers pages within the document; the why-panel links to the file."""
    library, source_file, _ = document
    later = Document(
        library_id=library.id, source_file_id=source_file.id, page_start=2, page_end=2
    )
    # Shrink the first document so the ranges do not overlap.
    first = (
        await session.execute(
            sa.select(Document).where(Document.source_file_id == source_file.id)
        )
    ).scalar_one()
    first.page_end = 1
    session.add(later)
    await session.commit()

    await _classify(session, later)
    classification = (
        await session.execute(
            sa.select(Classification).where(Classification.document_id == later.id)
        )
    ).scalar_one()
    provenance = (
        await session.execute(
            sa.select(FieldProvenance).where(
                FieldProvenance.classification_id == classification.id,
                FieldProvenance.field_name == "document_date",
            )
        )
    ).scalar_one()
    # Page 1 of this document is page 2 of the file.
    assert provenance.page_number == 2


async def test_model_and_prompt_version_are_recorded(session, document) -> None:
    """REQ-050 / REQ-113 — 'reprocess everything on v1' is a query."""
    _, _, doc = document
    await _classify(session, doc)

    rows = (
        await session.execute(
            sa.select(Classification.model, Classification.prompt_version)
            .where(Classification.prompt_version == "v1")
        )
    ).all()
    assert ("recorded", "v1") in rows


async def test_cache_health_is_recorded(session, document) -> None:
    """REQ-053 — a silent cache invalidation shows up as a number, not a bill."""
    _, _, doc = document
    await _classify(session, doc)

    classification = (
        await session.execute(
            sa.select(Classification).where(Classification.document_id == doc.id)
        )
    ).scalar_one()
    assert classification.usage["cache_read_input_tokens"] > 0


async def test_classification_is_audited_as_ai_authored(session, document) -> None:
    _, _, doc = document
    await _classify(session, doc)

    event = (
        await session.execute(
            sa.select(AuditEvent).where(
                AuditEvent.entity_id == doc.id, AuditEvent.action == "classify"
            )
        )
    ).scalar_one()
    assert event.actor_type is ActorType.AI


async def test_classification_queues_the_rules_stage(session, document) -> None:
    _, _, doc = document
    await _classify(session, doc)

    queued = (
        await session.execute(
            sa.select(Job).where(Job.document_id == doc.id, Job.stage == JobStage.RULES.value)
        )
    ).scalar_one()
    assert queued is not None


# --------------------------------------------------------------------------
# Degradation (REQ-055, REQ-056)
# --------------------------------------------------------------------------


async def test_with_no_provider_the_document_stays_searchable(session, document) -> None:
    """REQ-055. Retrieval never depends on a third-party API being up."""
    _, source_file, doc = document
    document_id, source_file_id = doc.id, source_file.id
    set_provider(UnavailableProvider())

    with pytest.raises(ProviderUnavailableError):
        await run_classify(session, _job(document_id))
    await session.rollback()

    # The pages — the thing search actually reads — are untouched.
    hit = (
        await session.execute(
            sa.select(Page.page_number).where(
                Page.source_file_id == source_file_id,
                Page.text_tsv.op("@@")(sa.func.websearch_to_tsquery("english", "declarations")),
            )
        )
    ).scalars().all()
    assert hit == [1]

    fresh = await session.get(Document, document_id)
    assert fresh.review_state is ReviewState.PENDING_CLASSIFICATION


async def test_classification_completes_once_the_provider_returns(session, document) -> None:
    """REQ-056 — the outage resolves without human action."""
    _, _, doc = document
    document_id = doc.id
    set_provider(UnavailableProvider())
    with pytest.raises(ProviderUnavailableError):
        await run_classify(session, _job(document_id))
    await session.rollback()

    fresh = await session.get(Document, document_id)
    await _classify(session, fresh)
    await session.refresh(fresh)
    assert fresh.title == "GEICO - Declarations - 4417"


async def test_a_superseded_document_is_skipped_not_failed(session, document) -> None:
    """Boundaries moved under the job. Its replacement has its own."""
    from datetime import UTC, datetime

    _, _, doc = document
    doc.superseded_at = datetime.now(UTC)
    await session.commit()

    set_provider(RecordedProvider(default=RESPONSE))
    await run_classify(session, _job(doc.id))  # must not raise
    await session.commit()

    assert (
        await session.execute(
            sa.select(sa.func.count()).select_from(Classification)
            .where(Classification.document_id == doc.id)
        )
    ).scalar_one() == 0


# --------------------------------------------------------------------------
# Candidates (REQ-047)
# --------------------------------------------------------------------------


async def test_the_prompt_carries_candidates_not_the_whole_taxonomy(
    session, document
) -> None:
    library, _, doc = document
    for index in range(3):
        session.add(Tag(library_id=library.id, name=f"tag-{index}", slug=f"tag-{index}"))
    await session.commit()

    provider = await _classify(session, doc)

    assert len(provider.requests) == 1
    request = provider.requests[0]
    # Cold start: no embedded neighbours yet, so the full (small) list is used.
    assert {candidate.name for candidate in request.tags} == {"tag-0", "tag-1", "tag-2"}


async def test_neighbour_tags_are_ranked_by_how_many_neighbours_use_them(
    session, document
) -> None:
    """The signal dumping the whole taxonomy cannot give: likelihood."""
    library, source_file, doc = document

    # A previously filed, very similar document carrying a tag.
    shared_tag = Tag(library_id=library.id, name="insurance", slug="insurance")
    rare_tag = Tag(library_id=library.id, name="obscure", slug="obscure")
    session.add_all([shared_tag, rare_tag])
    await session.flush()

    neighbour_file = SourceFile(
        library_id=library.id, sha256=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        byte_size=10, original_filename="older.pdf",
        ingest_source=IngestSource.WEB_UPLOAD, page_count=1,
        state=SourceFileState.PROCESSED,
    )
    session.add(neighbour_file)
    await session.flush()
    session.add(Page(source_file_id=neighbour_file.id, page_number=1, text=PAGES[1]))
    neighbour = Document(
        library_id=library.id, source_file_id=neighbour_file.id,
        page_start=1, page_end=1, review_state=ReviewState.FILED,
    )
    session.add(neighbour)
    await session.flush()
    session.add(DocumentTag(document_id=neighbour.id, tag_id=shared_tag.id, source=TagSource.AI))
    await session.commit()

    # Embed both, so the neighbour is findable by vector distance.
    await run_embed(
        session,
        ClaimedJob(id=uuid.uuid4(), stage=JobStage.EMBED, source_file_id=neighbour_file.id,
                   document_id=None, prompt_version=None, attempts=1),
    )
    await run_embed(
        session,
        ClaimedJob(id=uuid.uuid4(), stage=JobStage.EMBED, source_file_id=source_file.id,
                   document_id=None, prompt_version=None, attempts=1),
    )
    await session.commit()

    provider = await _classify(session, doc)
    request = provider.requests[0]

    names = [candidate.name for candidate in request.tags]
    assert names == ["insurance"], "only the neighbour's tags should be offered"
    assert request.tags[0].neighbour_count == 1


async def test_a_known_form_is_stated_as_fact_in_the_prompt(session, document) -> None:
    _, _, doc = document
    form = KnownForm(
        code="INS-DEC", name="Insurance Declarations Page",
        match_rules={}, field_extractors={"policy_number": "string"},
    )
    session.add(form)
    await session.flush()
    doc.known_form_id = form.id
    await session.commit()

    provider = await _classify(session, doc)
    request = provider.requests[0]
    assert request.known_form_code == "INS-DEC"
    assert request.known_form_fields == ["policy_number"]
