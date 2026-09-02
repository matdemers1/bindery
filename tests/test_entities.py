"""Phase 5 — entities, merge, assets, shelves (T-5.1 to T-5.11).

Merge is what this phase is really about: an operation that rewrites link rows
across thousands of documents and must undo as a single action. Alias resolution
is what keeps it rare.
"""

import uuid

import pytest
import sqlalchemy as sa

from api import entities
from api.db.enums import AssetKind, IngestSource, SourceFileState, TagSource
from api.db.models import (
    Asset,
    Correspondent,
    Document,
    DocumentAsset,
    DocumentTag,
    DocumentType,
    Page,
    SourceFile,
    Tag,
)


@pytest.fixture
async def library_with_documents(session, signed_in):
    _, library = await signed_in()
    source_file = SourceFile(
        library_id=library.id, sha256=uuid.uuid4().hex * 2, byte_size=10,
        original_filename="records.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        page_count=10, state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    await session.flush()
    for n in range(1, 11):
        session.add(Page(source_file_id=source_file.id, page_number=n, text=f"page {n} text"))
    documents = [
        Document(library_id=library.id, source_file_id=source_file.id,
                 page_start=n, page_end=n, title=f"Doc {n}")
        for n in range(1, 11)
    ]
    session.add_all(documents)
    await session.commit()
    return library, documents


# --------------------------------------------------------------------------
# Aliases (T-5.1, REQ-071)
# --------------------------------------------------------------------------


async def test_three_spellings_resolve_to_one_correspondent(
    session, library_with_documents
) -> None:
    """The whole point: this is what keeps merge rare rather than constant."""
    library, _ = library_with_documents
    honda = Correspondent(
        library_id=library.id, name="American Honda Finance", slug="american-honda-finance"
    )
    session.add(honda)
    await session.flush()
    for alias in ("Honda Financial Services", "Honda Fin Svcs"):
        await entities.add_alias(session, honda, alias)
    await session.commit()

    for spelling in (
        "American Honda Finance", "Honda Financial Services", "Honda Fin Svcs",
        "HONDA FIN SVCS", "honda-fin-svcs",
    ):
        found = await entities.resolve_correspondent(session, spelling, library.id)
        assert found is not None and found.id == honda.id, spelling


async def test_an_unknown_name_resolves_to_nothing(session, library_with_documents) -> None:
    library, _ = library_with_documents
    assert await entities.resolve_correspondent(session, "Progressive", library.id) is None


async def test_the_classifier_reuses_a_correspondent_via_its_alias(
    session, library_with_documents
) -> None:
    """A new_name that matches an alias must not create a near-duplicate."""
    from worker.ai.provider import ClassificationResult, TaxonomyChoice
    from worker.classify.resolve import resolve

    library, documents = library_with_documents
    honda = Correspondent(library_id=library.id, name="American Honda Finance",
                          slug="american-honda-finance")
    session.add(honda)
    await session.flush()
    await entities.add_alias(session, honda, "Honda Fin Svcs")
    await session.commit()

    result = ClassificationResult(
        title="x", summary="y",
        correspondent=TaxonomyChoice(new_name="Honda Fin Svcs"),
    )
    resolution = await resolve(session, documents[0], result, [library.id])

    assert resolution.correspondent_id == honda.id
    # And it counts as reuse at the gate, not invention.
    assert resolution.correspondent_was_existing is True
    assert resolution.invented_names == []


# --------------------------------------------------------------------------
# Merge (T-5.2, REQ-072)
# --------------------------------------------------------------------------


@pytest.fixture
async def two_correspondents(session, library_with_documents):
    library, documents = library_with_documents
    a = Correspondent(library_id=library.id, name="Honda Finance", slug="honda-finance")
    b = Correspondent(library_id=library.id, name="American Honda Finance",
                      slug="american-honda-finance")
    session.add_all([a, b])
    await session.flush()
    for document in documents[:4]:
        document.correspondent_id = a.id
    for document in documents[4:6]:
        document.correspondent_id = b.id
    await session.commit()
    return library, documents, a, b


async def test_a_merge_preview_writes_nothing(client, session, two_correspondents) -> None:
    _, _, a, b = two_correspondents
    body = (await client.post("/api/correspondents/merge/preview", json={
        "source_id": str(a.id), "target_id": str(b.id)
    })).json()

    assert body["document_count"] == 4
    assert body["from_name"] == "Honda Finance"
    assert body["operation_id"] is None

    await session.refresh(a)
    assert a.merged_at is None


async def test_merge_moves_every_document_and_tombstones_the_source(
    client, session, two_correspondents
) -> None:
    _, _, a, b = two_correspondents
    body = (await client.post("/api/correspondents/merge", json={
        "source_id": str(a.id), "target_id": str(b.id)
    })).json()
    assert body["operation_id"] is not None

    await session.refresh(a)
    assert a.merged_into_id == b.id
    assert a.merged_at is not None

    moved = (
        await session.execute(
            sa.select(sa.func.count()).select_from(Document)
            .where(Document.correspondent_id == b.id)
        )
    ).scalar_one()
    assert moved == 6


async def test_the_merged_away_name_still_resolves(client, session, two_correspondents) -> None:
    """Old references must land somewhere, not dangle."""
    library, _, a, b = two_correspondents
    await client.post("/api/correspondents/merge",
                      json={"source_id": str(a.id), "target_id": str(b.id)})

    found = await entities.resolve_correspondent(session, "Honda Finance", library.id)
    assert found is not None and found.id == b.id


async def test_merge_undoes_records_and_links_in_one_action(
    client, session, two_correspondents
) -> None:
    """REQ-072 — undo restores both records **and** every link row."""
    _, _, a, b = two_correspondents
    operation = (await client.post("/api/correspondents/merge", json={
        "source_id": str(a.id), "target_id": str(b.id)
    })).json()["operation_id"]

    result = (await client.post(f"/api/merges/{operation}/undo")).json()
    assert result["restored"] == 4

    await session.refresh(a)
    assert a.merged_into_id is None
    back = (
        await session.execute(
            sa.select(sa.func.count()).select_from(Document)
            .where(Document.correspondent_id == a.id)
        )
    ).scalar_one()
    assert back == 4


async def test_a_merge_cannot_be_undone_twice(client, two_correspondents) -> None:
    _, _, a, b = two_correspondents
    operation = (await client.post("/api/correspondents/merge", json={
        "source_id": str(a.id), "target_id": str(b.id)
    })).json()["operation_id"]
    assert (await client.post(f"/api/merges/{operation}/undo")).status_code == 200
    assert (await client.post(f"/api/merges/{operation}/undo")).status_code == 409


async def test_merging_a_correspondent_into_itself_is_refused(
    client, two_correspondents
) -> None:
    _, _, a, _ = two_correspondents
    response = await client.post("/api/correspondents/merge",
                                 json={"source_id": str(a.id), "target_id": str(a.id)})
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Merge across the library boundary (ADR-005)
# --------------------------------------------------------------------------


async def test_a_correspondent_id_is_not_a_capability_across_libraries(
    client, session, two_correspondents, signed_in
) -> None:
    """An id seen once — in an export, an audit blob, a URL — must not stay
    usable after the membership that showed it has gone. 404, never 403: a 403
    would confirm the record exists, which is the whole answer a probe wants."""
    _, documents, a, b = two_correspondents
    await client.post("/api/auth/logout")
    await signed_in(library_name="Someone Else")

    preview = await client.post("/api/correspondents/merge/preview",
                                json={"source_id": str(a.id), "target_id": str(b.id)})
    assert preview.status_code == 404
    assert documents[0].title not in preview.text, "a preview lists document titles"

    merge = await client.post("/api/correspondents/merge",
                              json={"source_id": str(a.id), "target_id": str(b.id)})
    assert merge.status_code == 404

    await session.refresh(a)
    assert a.merged_at is None, "another library's taxonomy is untouched"


async def test_a_tag_id_is_not_a_capability_across_libraries(
    client, session, library_with_documents, signed_in
) -> None:
    library, _ = library_with_documents
    old = Tag(library_id=library.id, name="reciept", slug=f"r-{uuid.uuid4().hex[:6]}")
    new = Tag(library_id=library.id, name="receipt", slug=f"n-{uuid.uuid4().hex[:6]}")
    session.add_all([old, new])
    await session.commit()

    await client.post("/api/auth/logout")
    await signed_in(library_name="Someone Else")

    response = await client.post("/api/tags/merge",
                                 json={"source_id": str(old.id), "target_id": str(new.id)})
    assert response.status_code == 404

    await session.refresh(old)
    assert old.merged_at is None


async def test_an_operation_id_is_not_a_capability_across_libraries(
    client, session, two_correspondents, signed_in
) -> None:
    """Undo rewrites the same rows the merge did, so it needs the same boundary."""
    _, _, a, b = two_correspondents
    operation = (await client.post("/api/correspondents/merge", json={
        "source_id": str(a.id), "target_id": str(b.id)
    })).json()["operation_id"]

    await client.post("/api/auth/logout")
    await signed_in(library_name="Someone Else")

    assert (await client.post(f"/api/merges/{operation}/undo")).status_code == 404

    await session.refresh(a)
    assert a.merged_into_id == b.id, "the merge still stands"
    still = (
        await session.execute(
            sa.select(sa.func.count()).select_from(Document)
            .where(Document.correspondent_id == b.id)
        )
    ).scalar_one()
    assert still == 6


# --------------------------------------------------------------------------
# Tag merge (T-5.5, REQ-076)
# --------------------------------------------------------------------------


async def test_merging_tags_retags_every_document_and_undoes_as_one(
    client, session, library_with_documents
) -> None:
    from api.db.models import live_tag_links

    library, documents = library_with_documents
    old = Tag(library_id=library.id, name="reciept", slug="reciept")   # a real typo
    new = Tag(library_id=library.id, name="receipt", slug="receipt")
    session.add_all([old, new])
    await session.flush()
    for document in documents[:5]:
        session.add(DocumentTag(document_id=document.id, tag_id=old.id, source=TagSource.AI))
    await session.commit()

    operation = (await client.post("/api/tags/merge", json={
        "source_id": str(old.id), "target_id": str(new.id)
    })).json()["operation_id"]

    live_new = (
        await session.execute(
            sa.select(sa.func.count()).select_from(DocumentTag)
            .where(DocumentTag.tag_id == new.id, live_tag_links())
        )
    ).scalar_one()
    live_old = (
        await session.execute(
            sa.select(sa.func.count()).select_from(DocumentTag)
            .where(DocumentTag.tag_id == old.id, live_tag_links())
        )
    ).scalar_one()
    assert (live_new, live_old) == (5, 0)

    await client.post(f"/api/merges/{operation}/undo")
    restored = (
        await session.execute(
            sa.select(sa.func.count()).select_from(DocumentTag)
            .where(DocumentTag.tag_id == old.id, live_tag_links())
        )
    ).scalar_one()
    assert restored == 5


# --------------------------------------------------------------------------
# Assets and the timeline (T-5.3, T-5.4)
# --------------------------------------------------------------------------


async def test_an_asset_carries_typed_attributes(client, library_with_documents) -> None:
    library, _ = library_with_documents
    body = (await client.post("/api/assets", json={
        "library_id": str(library.id), "kind": "vehicle", "name": "2020 Honda Accord",
        "attributes": {"vin": "1HGCV1F34LA012345", "plate": "ABC-1234"},
    })).json()
    assert body["kind"] == "vehicle"
    assert body["attributes"]["vin"] == "1HGCV1F34LA012345"


async def test_one_document_can_concern_two_assets(client, session, library_with_documents) -> None:
    """An insurance declarations page covers both cars."""
    library, documents = library_with_documents
    ids = []
    for name in ("Honda Accord", "Toyota Tacoma"):
        body = (await client.post("/api/assets", json={
            "library_id": str(library.id), "kind": "vehicle", "name": name, "attributes": {},
        })).json()
        ids.append(body["id"])

    for asset_id in ids:
        assert (
            await client.post(f"/api/assets/{asset_id}/documents/{documents[0].id}")
        ).status_code == 200

    attached = (
        await session.execute(
            sa.select(sa.func.count()).select_from(DocumentAsset)
            .where(DocumentAsset.document_id == documents[0].id,
                   DocumentAsset.removed_at.is_(None))
        )
    ).scalar_one()
    assert attached == 2


async def test_the_timeline_is_chronological_by_the_documents_own_date(
    client, session, library_with_documents
) -> None:
    """A receipt filed years late belongs at its date, not at the day it was scanned."""
    from datetime import date

    library, documents = library_with_documents
    asset = Asset(library_id=library.id, kind=AssetKind.VEHICLE, name="Honda", slug="honda")
    session.add(asset)
    await session.flush()

    for document, when in zip(
        documents[:3], [date(2021, 5, 1), date(2019, 3, 2), date(2023, 8, 9)], strict=False
    ):
        document.document_date = when
        session.add(DocumentAsset(document_id=document.id, asset_id=asset.id,
                                  source=TagSource.HUMAN))
    await session.commit()

    body = (await client.get(f"/api/assets/{asset.id}/timeline")).json()
    dates = [entry["date"] for entry in body["entries"]]
    assert dates == ["2023-08-09", "2021-05-01", "2019-03-02"]


# --------------------------------------------------------------------------
# Taxonomy health (T-5.6, REQ-077) and duplicates (T-5.10)
# --------------------------------------------------------------------------


async def test_health_flags_a_seeded_near_duplicate_pair(
    client, session, library_with_documents
) -> None:
    library, _ = library_with_documents
    session.add_all([
        Tag(library_id=library.id, name="insurance", slug="insurance"),
        Tag(library_id=library.id, name="insurances", slug="insurances"),
    ])
    await session.commit()

    body = (await client.get("/api/taxonomy/health")).json()
    pairs = {(p["a_name"], p["b_name"]) for p in body["near_duplicate_tags"]}
    assert ("insurance", "insurances") in pairs or ("insurances", "insurance") in pairs


async def test_health_reports_the_r08_orphan_ratio(client, session, library_with_documents) -> None:
    """R-08: >15% of tags used exactly once means the taxonomy is drifting."""
    library, documents = library_with_documents
    for n in range(25):
        tag = Tag(library_id=library.id, name=f"orphan-{n}", slug=f"orphan-{n}")
        session.add(tag)
        await session.flush()
        session.add(DocumentTag(document_id=documents[0].id, tag_id=tag.id,
                                source=TagSource.AI))
    await session.commit()

    body = (await client.get("/api/taxonomy/health")).json()
    assert body["total_tags"] >= 25
    assert body["orphan_ratio"] > 0.15
    assert body["exceeds_alarm"] is True


async def test_duplicate_detection_records_but_never_resolves(
    client, session, library_with_documents
) -> None:
    """Two scans of one deed are both worth keeping until a human says otherwise."""
    from api.embedding import get_embedding_provider

    _, documents = library_with_documents
    provider = get_embedding_provider()
    identical = provider.embed("certificate of title vehicle identification number odometer")
    documents[0].embedding = identical
    documents[1].embedding = identical
    await session.commit()

    body = (await client.post("/api/duplicates/scan")).json()
    assert body["found"] >= 1

    listed = (await client.get("/api/duplicates")).json()
    assert len(listed) >= 1
    # Both documents still exist. Nothing was resolved.
    for document in documents[:2]:
        await session.refresh(document)
        assert document.superseded_at is None


async def test_find_similar_uses_the_classification_embeddings(
    client, session, library_with_documents
) -> None:
    from api.embedding import get_embedding_provider

    _, documents = library_with_documents
    provider = get_embedding_provider()
    documents[0].embedding = provider.embed("geico automobile insurance declarations honda")
    documents[1].embedding = provider.embed("geico automobile insurance declarations honda accord")
    documents[2].embedding = provider.embed("department of defense certificate of discharge")
    await session.commit()

    body = (await client.get(f"/api/documents/{documents[0].id}/similar")).json()
    assert body["results"][0]["document_id"] == str(documents[1].id)
    assert body["results"][0]["similarity"] > 0.5


# --------------------------------------------------------------------------
# Shelves (T-5.8)
# --------------------------------------------------------------------------


async def test_a_shelf_is_a_saved_query(client, library_with_documents) -> None:
    library, _ = library_with_documents
    created = (await client.post("/api/shelves", json={
        "library_id": str(library.id), "name": "2026 Tax Return",
        "query": {"tag": ["tax"], "year": 2026}, "is_packet": True,
    })).json()
    assert created["is_packet"] is True

    listed = (await client.get("/api/shelves")).json()
    assert [s["name"] for s in listed] == ["2026 Tax Return"]


async def test_entities_are_library_scoped(client, library_with_documents, signed_in) -> None:
    await client.post("/api/auth/logout")
    await signed_in(library_name="Someone Else")
    assert (await client.get("/api/correspondents")).json() == []
    assert (await client.get("/api/assets")).json() == []
    assert (await client.get("/api/shelves")).json() == []
    assert (await client.get("/api/duplicates")).json() == []


# --------------------------------------------------------------------------
# Unifying correspondents (T-8.17)
# --------------------------------------------------------------------------


def test_a_group_of_one_merges_nothing() -> None:
    """A proposal naming a single record is not a merge, it is noise."""
    from api.unify import _parse

    names = [{"id": str(uuid.uuid4()), "name": "26th Weapons Squadron", "documents": 7}]
    proposal = _parse(
        '{"groups": [{"canonical": "26th Weapons Squadron", '
        '"members": ["26th Weapons Squadron"], "reason": "same"}]}',
        names, "claude-opus-5",
    )
    assert proposal.groups == []


def test_a_name_the_archive_does_not_have_is_dropped() -> None:
    """The task was to group what exists.

    Inventing a folder is the opposite of tidying, so an unrecognised name is
    discarded rather than created.
    """
    from api.unify import _parse

    real = str(uuid.uuid4())
    other = str(uuid.uuid4())
    names = [
        {"id": real, "name": "26th Weapons Squadron", "documents": 7},
        {"id": other, "name": "26th Weapon School", "documents": 2},
    ]
    proposal = _parse(
        '{"groups": [{"canonical": "26th Weapons Squadron", '
        '"members": ["26th Weapons Squadron", "26th Weapon School", '
        '"Something Never Filed"], "reason": "same unit"}]}',
        names, "claude-opus-5",
    )
    assert len(proposal.groups) == 1
    assert {m["name"] for m in proposal.groups[0].members} == {
        "26th Weapons Squadron", "26th Weapon School"
    }


def test_an_invented_canonical_falls_back_to_the_biggest_member() -> None:
    """If the survivor named does not exist, the merge is still correct — only
    the label would have been prettier. Keeping the record carrying the most
    documents moves the fewest things."""
    from api.unify import _parse

    big, small = str(uuid.uuid4()), str(uuid.uuid4())
    names = [
        {"id": big, "name": "26th Weapons Squadron", "documents": 7},
        {"id": small, "name": "26th Weapon School", "documents": 2},
    ]
    proposal = _parse(
        '{"groups": [{"canonical": "26th Weapons Squadron (USAF)", '
        '"members": ["26th Weapons Squadron", "26th Weapon School"], "reason": "x"}]}',
        names, "claude-opus-5",
    )
    assert str(proposal.groups[0].canonical_id) == big


def test_an_unreadable_response_is_reported_not_guessed_at() -> None:
    from api.unify import _parse

    proposal = _parse("I'm not sure how to answer that.", [], None)
    assert proposal.groups == []
    assert "could not be read" in (proposal.unavailable_reason or "")


async def test_nothing_is_proposed_without_a_key(session, signed_in) -> None:
    """The trigram near-duplicate list works without one; this does not, and
    says so rather than returning an empty result that looks like agreement."""
    from api import unify

    _, library = await signed_in()
    session.add_all([
        Correspondent(library_id=library.id, name="A Bank", slug=f"a-{uuid.uuid4().hex[:6]}"),
        Correspondent(library_id=library.id, name="A Bank NA", slug=f"b-{uuid.uuid4().hex[:6]}"),
    ])
    await session.commit()

    proposal = await unify.propose(session, [library.id], None)
    assert proposal.groups == []
    assert "No API key" in (proposal.unavailable_reason or "")
    assert proposal.considered == 2


# --------------------------------------------------------------------------
# Unifying types and tags, not only correspondents (T-9.2)
# --------------------------------------------------------------------------


class _Answerer:
    """Returns a fixed grouping, so the test is about the plumbing not the model."""

    model = "claude-sonnet-5"

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.asked: list[str] = []

    def available(self) -> bool:
        return True

    async def complete(self, prompt: str, *, max_tokens: int = 4000) -> str:
        self.asked.append(prompt)
        self.budget = max_tokens
        return self.payload


async def test_document_types_can_be_unified(session, signed_in) -> None:
    """166 of 277 types were used exactly once when R-08 fired.

    `Unit Patch`, `Unit Patch Image`, `Unit Emblem` and `Insignia` are one kind
    of document described four ways.
    """
    from api import unify

    _, library = await signed_in()
    names = ["Unit Patch", "Unit Patch Image", "Unit Emblem"]
    for name in names:
        session.add(
            DocumentType(library_id=library.id, name=name, slug=f"{uuid.uuid4().hex[:8]}")
        )
    await session.commit()

    answerer = _Answerer(
        '{"groups": [{"canonical": "Unit Patch", '
        '"members": ["Unit Patch", "Unit Patch Image", "Unit Emblem"], '
        '"reason": "three phrasings of a squadron patch"}]}'
    )
    proposal = await unify.propose(session, [library.id], answerer, "document_type")

    assert proposal.kind == "document_type"
    assert len(proposal.groups) == 1
    assert proposal.groups[0].canonical == "Unit Patch"
    assert len(proposal.groups[0].members) == 3
    # The prompt has to be the one written for types, not the organisation one.
    assert "document *kinds*" in answerer.asked[0]
    assert "squadron and its wing" not in answerer.asked[0]


async def test_tags_can_be_unified(session, signed_in) -> None:
    from api import unify

    _, library = await signed_in()
    for name in ["receipt", "receipts"]:
        session.add(Tag(library_id=library.id, name=name, slug=f"{uuid.uuid4().hex[:8]}"))
    await session.commit()

    answerer = _Answerer(
        '{"groups": [{"canonical": "receipts", "members": ["receipt", "receipts"], '
        '"reason": "singular and plural"}]}'
    )
    proposal = await unify.propose(session, [library.id], answerer, "tag")

    assert proposal.kind == "tag"
    assert [m["name"] for m in proposal.groups[0].members] == ["receipt", "receipts"]
    assert "tag vocabulary" in answerer.asked[0]
    # The hierarchy rule is the one that keeps `medical` and `dental` apart.
    assert "Do not collapse a hierarchy into its root" in answerer.asked[0]


async def test_merging_a_document_type_moves_its_documents_and_is_undoable(
    session, signed_in
) -> None:
    """Document types had no merge at all until T-9.2 — the one kind of taxonomy
    the model invents most freely was the only kind that could not be tidied."""
    from api import entities

    user, library = await signed_in()
    source = DocumentType(
        library_id=library.id, name="Unit Patch Image", slug=f"s-{uuid.uuid4().hex[:6]}"
    )
    target = DocumentType(
        library_id=library.id, name="Unit Patch", slug=f"t-{uuid.uuid4().hex[:6]}"
    )
    source_file = SourceFile(
        library_id=library.id, sha256=uuid.uuid4().hex * 2, byte_size=1,
        original_filename="patch.png", ingest_source=IngestSource.WEB_UPLOAD,
    )
    session.add_all([source, target, source_file])
    await session.flush()
    document = Document(
        library_id=library.id, source_file_id=source_file.id,
        page_start=1, page_end=1, document_type_id=source.id,
    )
    session.add(document)
    await session.commit()

    event_id = await entities.merge_document_types(
        session, source.id, target.id, actor_id=user.id
    )
    await session.commit()
    await session.refresh(document)
    await session.refresh(source)

    assert document.document_type_id == target.id
    assert source.merged_into_id == target.id, "tombstoned, not deleted (REQ-090)"

    restored = await entities.undo_merge(session, event_id, actor_id=user.id)
    await session.commit()
    await session.refresh(document)
    await session.refresh(source)

    assert restored == 1
    assert document.document_type_id == source.id
    assert source.merged_into_id is None


async def test_a_merged_away_type_is_not_offered_for_merging_again(
    session, signed_in
) -> None:
    from api import entities, unify

    user, library = await signed_in()
    source = DocumentType(
        library_id=library.id, name="Gone", slug=f"g-{uuid.uuid4().hex[:6]}"
    )
    target = DocumentType(
        library_id=library.id, name="Kept", slug=f"k-{uuid.uuid4().hex[:6]}"
    )
    session.add_all([source, target])
    await session.commit()

    await entities.merge_document_types(session, source.id, target.id, actor_id=user.id)
    await session.commit()

    listed = await unify._current_names(session, unify.KINDS["document_type"], [library.id])
    assert [entry["name"] for entry in listed] == ["Kept"]


async def test_a_truncated_answer_says_it_was_truncated(session, signed_in) -> None:
    """JSON cut off mid-string is invalid JSON.

    The first real run over 277 document types hit the 4,000-token default and
    was reported as "the answer could not be read" — true, and it sends you to
    look at the parser rather than at max_tokens.
    """
    from api import unify
    from api.ai_ask import TruncatedAnswerError

    _, library = await signed_in()
    for name in ["One", "Two"]:
        session.add(
            DocumentType(library_id=library.id, name=name, slug=f"{uuid.uuid4().hex[:8]}")
        )
    await session.commit()

    class Cut:
        model = "claude-sonnet-5"

        def available(self) -> bool:
            return True

        async def complete(self, prompt: str, *, max_tokens: int = 4000) -> str:
            raise TruncatedAnswerError(f"the answer was cut off at {max_tokens} tokens")

    proposal = await unify.propose(session, [library.id], Cut(), "document_type")

    assert proposal.groups == []
    assert "cut off" in (proposal.unavailable_reason or "")


async def test_the_unify_pass_asks_for_room_to_answer(session, signed_in) -> None:
    """A taxonomy with hundreds of near-duplicates produces a long answer, and
    output tokens are the cheap half."""
    from api import unify

    _, library = await signed_in()
    for name in ["One", "Two"]:
        session.add(
            DocumentType(library_id=library.id, name=name, slug=f"{uuid.uuid4().hex[:8]}")
        )
    await session.commit()

    answerer = _Answerer('{"groups": []}')
    await unify.propose(session, [library.id], answerer, "document_type")

    assert answerer.budget == unify.MAX_ANSWER_TOKENS
    assert unify.MAX_ANSWER_TOKENS >= 16000


async def test_a_long_list_is_asked_in_chunks(session, signed_in) -> None:
    """697 tags in one request came back with zero characters of text.

    The whole output budget went on reasoning about a list that long before a
    single group was written. Chunks are cut alphabetically — the ordering that
    would be wrong for the candidate list and is right here, because tag
    duplication is overwhelmingly lexical.
    """
    from api import unify

    _, library = await signed_in()
    for n in range(unify.CHUNK_SIZE + 50):
        session.add(
            Tag(library_id=library.id, name=f"tag-{n:04d}", slug=f"{uuid.uuid4().hex[:8]}")
        )
    await session.commit()

    answerer = _Answerer('{"groups": []}')
    await unify.propose(session, [library.id], answerer, "tag")

    assert len(answerer.asked) > 1, "a list past the threshold is split"
    seen = sum(prompt.count("- tag-") for prompt in answerer.asked)
    assert seen >= unify.CHUNK_SIZE + 50, "every name is asked about at least once"


async def test_one_failed_chunk_does_not_discard_the_others(
    session, signed_in
) -> None:
    """A partial answer is worth more than nothing, and this runs against a
    live model that fails intermittently on long lists."""
    from api import unify

    _, library = await signed_in()
    for n in range(unify.CHUNK_SIZE + 50):
        session.add(
            Tag(library_id=library.id, name=f"tag-{n:04d}", slug=f"{uuid.uuid4().hex[:8]}")
        )
    await session.commit()

    class FailsTheSecond:
        model = "claude-sonnet-5"

        def __init__(self) -> None:
            self.calls = 0

        def available(self) -> bool:
            return True

        async def complete(self, prompt: str, *, max_tokens: int = 4000) -> str:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("cut off")
            return (
                '{"groups": [{"canonical": "tag-0000", '
                '"members": ["tag-0000", "tag-0001"], "reason": "test"}]}'
            )

    proposal = await unify.propose(session, [library.id], FailsTheSecond(), "tag")

    assert proposal.unavailable_reason is None
    assert proposal.groups, "the chunk that answered still counts"


async def test_the_overlap_does_not_propose_the_same_name_twice(
    session, signed_in
) -> None:
    """A name in two chunks would be merged twice, and the second is a
    self-merge into a record that no longer exists as a target."""
    from api import unify

    _, library = await signed_in()
    for n in range(unify.CHUNK_SIZE + 50):
        session.add(
            Tag(library_id=library.id, name=f"tag-{n:04d}", slug=f"{uuid.uuid4().hex[:8]}")
        )
    await session.commit()

    # Every chunk proposes the same pair, which only the overlap could produce.
    answerer = _Answerer(
        '{"groups": [{"canonical": "tag-0000", '
        '"members": ["tag-0000", "tag-0001"], "reason": "test"}]}'
    )
    proposal = await unify.propose(session, [library.id], answerer, "tag")

    assert len(proposal.groups) == 1, "claimed once, not once per chunk"


# --------------------------------------------------------------------------
# Guards learned from over-merging a real archive (T-9.3)
# --------------------------------------------------------------------------


def _names(*pairs):
    return [{"id": str(uuid.uuid4()), "name": n, "documents": d} for n, d in pairs]


def test_a_general_kind_is_never_dissolved_into_a_specific_one() -> None:
    """The merge that did the most damage.

    `Receipt` was merged into `Veterinary Receipt`, `Data Export` into `Survey
    Data Export`, `Presentation` into `Training Presentation`. In each the
    survivor carried strictly more words: the general kind was dissolved into a
    special case, because the special case happened to have more documents and
    the prompt said to prefer the name the archive had settled on.
    """
    from api.unify import _parse

    names = _names(("Receipt", 3), ("Veterinary Receipt", 9))
    proposal = _parse(
        '{"groups": [{"canonical": "Veterinary Receipt", '
        '"members": ["Receipt", "Veterinary Receipt"], "reason": "both receipts"}]}',
        names, "m", "document_type",
    )

    assert proposal.groups == []


def test_a_rewording_is_still_merged() -> None:
    """The guard must not refuse the merges that are the point of the feature."""
    from api.unify import _parse

    names = _names(("Unit Emblem", 2), ("Unit Insignia", 3))
    proposal = _parse(
        '{"groups": [{"canonical": "Unit Insignia", '
        '"members": ["Unit Emblem", "Unit Insignia"], "reason": "same thing"}]}',
        names, "m", "document_type",
    )

    assert len(proposal.groups) == 1


def test_a_group_whose_reason_rejects_it_is_dropped() -> None:
    """A real response listed a group in order to explain it was not one.

    Only a trailing-comma parse error stopped it being applied, which is not a
    safeguard.
    """
    from api.unify import _parse

    names = _names(("Weapons Instructor Course", 4), ("weapons configuration", 1))
    proposal = _parse(
        '{"groups": [{"canonical": "Weapons Instructor Course", '
        '"members": ["Weapons Instructor Course", "weapons configuration"], '
        '"reason": "Left out - different concepts, not a true duplicate"}]}',
        names, "m", "document_type",
    )

    assert proposal.groups == []


def test_a_trailing_comma_does_not_discard_the_whole_answer() -> None:
    """One comma before `]}` cost every group in a real response."""
    from api.unify import _parse

    names = _names(("receipt", 2), ("receipts", 5))
    proposal = _parse(
        '{"groups": [\n  {"canonical": "receipts", '
        '"members": ["receipt", "receipts"], "reason": "plural"},\n]}',
        names, "m", "tag",
    )

    assert len(proposal.groups) == 1
    assert proposal.groups[0].canonical == "receipts"


async def test_merging_into_a_tag_a_document_once_had_and_lost(
    session, signed_in
) -> None:
    """`document_tag`'s primary key is `(document_id, tag_id)`, so a *removed*
    link still occupies that key.

    The duplicate check filtered on `removed_at IS NULL`, which reads naturally
    and is wrong: merging a tag into one a document had previously carried and
    lost collided on `pk_document_tag` and failed the whole merge. It needs a
    removal *and* a later merge into the same tag to reproduce — which is what
    a merge, an undo, and a second merge produce.
    """
    from datetime import UTC, datetime

    from api import entities

    user, library = await signed_in()
    source_file = SourceFile(
        library_id=library.id, sha256=uuid.uuid4().hex * 2, byte_size=1,
        original_filename="x.pdf", ingest_source=IngestSource.WEB_UPLOAD,
    )
    keep = Tag(library_id=library.id, name="receipts", slug=f"k-{uuid.uuid4().hex[:6]}")
    fold = Tag(library_id=library.id, name="receipt", slug=f"f-{uuid.uuid4().hex[:6]}")
    session.add_all([source_file, keep, fold])
    await session.flush()

    document = Document(
        library_id=library.id, source_file_id=source_file.id, page_start=1, page_end=1
    )
    session.add(document)
    await session.flush()

    # The document carried the survivor once and had it removed…
    session.add(
        DocumentTag(
            document_id=document.id, tag_id=keep.id, source=TagSource.AI,
            removed_at=datetime.now(UTC),
        )
    )
    # …and carries the one about to be merged into it.
    session.add(
        DocumentTag(document_id=document.id, tag_id=fold.id, source=TagSource.AI)
    )
    await session.commit()

    await entities.merge_tags(session, fold.id, keep.id, actor_id=user.id)
    await session.commit()

    live = (
        await session.execute(
            sa.select(DocumentTag).where(
                DocumentTag.document_id == document.id,
                DocumentTag.tag_id == keep.id,
            )
        )
    ).scalar_one()
    assert live.removed_at is None, "the link is revived, not duplicated"
