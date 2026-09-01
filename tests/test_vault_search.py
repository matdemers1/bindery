"""Searching inside an unlocked vault (T-16.8, REQ-184).

The vault's search is a decrypt-and-scan, not an index, so the properties worth
pinning are the ones an index would have given for free: that it matches what
it should, that it stops at the boundary of one vault, and that a single
unreadable page cannot take the whole search down or pass unnoticed.
"""

import uuid

import pytest

from api.db.enums import IngestSource, ReviewState, SourceFileState
from api.db.models import Document, SourceFile, VaultItem, VaultPage
from api.vault import crypto, search, store

PHRASE = "brackenridge conveyance"


async def _document(session, library_id: uuid.UUID) -> Document:
    """A vaulted item points at a real document row, so the tests need one."""
    source = SourceFile(
        library_id=library_id,
        sha256=uuid.uuid4().hex * 2,
        byte_size=1,
        original_filename="deed.pdf",
        ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1,
        state=SourceFileState.PROCESSED,
    )
    session.add(source)
    await session.flush()
    document = Document(
        library_id=library_id,
        source_file_id=source.id,
        page_start=1,
        page_end=1,
        review_state=ReviewState.FILED,
    )
    session.add(document)
    await session.flush()
    return document


def _add(session, vault_id, document_id, key, texts, title="Deed"):
    import json

    item = VaultItem(
        document_id=document_id,
        vault_id=vault_id,
        object_name=store.new_object_name(),
        byte_size=1,
        sealed_sha256=crypto.encrypt(b"x" * 64, key),
        sealed_meta=crypto.encrypt(json.dumps({"title": title}).encode(), key),
        page_count=len(texts),
    )
    session.add(item)
    return item


@pytest.fixture
async def stocked(session, signed_in, tmp_path, monkeypatch):
    """Two vaults, so the boundary between them is testable."""
    from api.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    from api.vault import service

    mine, my_library = await signed_in()
    yours, your_library = await signed_in(
        email=f"other-{uuid.uuid4().hex[:8]}@example.test"
    )
    my_vault = await service.create(session, mine.id, "a-long-enough-passphrase", "481516")
    your_vault = await service.create(session, yours.id, "another-long-passphrase", "234223")
    await session.flush()
    my_key = service.require_key(mine.id)
    your_key = service.require_key(yours.id)

    mine_doc = (await _document(session, my_library.id)).id
    item = _add(session, my_vault.id, mine_doc, my_key, ["a", "b"], title="Deed")
    await session.flush()
    session.add(
        VaultPage(
            vault_item_id=item.id, page_number=1,
            sealed_text=crypto.encrypt(f"page one: {PHRASE} of record".encode(), my_key),
        )
    )
    session.add(
        VaultPage(
            vault_item_id=item.id, page_number=2,
            sealed_text=crypto.encrypt(b"page two: nothing of interest", my_key),
        )
    )

    theirs = _add(
        session,
        your_vault.id,
        (await _document(session, your_library.id)).id,
        your_key,
        ["a"],
        title="Theirs",
    )
    await session.flush()
    session.add(
        VaultPage(
            vault_item_id=theirs.id, page_number=1,
            sealed_text=crypto.encrypt(f"also mentions {PHRASE}".encode(), your_key),
        )
    )
    await session.commit()
    return my_vault, my_key, your_vault, your_key, mine_doc, item


async def test_it_finds_the_page(session, stocked):
    my_vault, my_key, *_ , mine_doc, _item = stocked
    found = await search.search(session, my_vault.id, "brackenridge", my_key)
    assert found.total == 1
    assert found.hits[0].page_number == 1
    assert found.hits[0].document_id == mine_doc


async def test_the_snippet_carries_the_match_and_the_title(session, stocked):
    """A hit list of untitled page numbers would not be usable."""
    my_vault, my_key, *_ = stocked
    found = await search.search(session, my_vault.id, "brackenridge", my_key)
    assert "brackenridge" in found.hits[0].snippet.lower()
    assert found.hits[0].title == "Deed"


async def test_every_term_must_match(session, stocked):
    my_vault, my_key, *_ = stocked
    assert (await search.search(session, my_vault.id, "brackenridge record", my_key)).total == 1
    assert (await search.search(session, my_vault.id, "brackenridge absent", my_key)).total == 0


async def test_it_stops_at_the_edge_of_one_vault(session, stocked):
    """Both vaults contain the phrase. Mine must return only mine."""
    my_vault, my_key, your_vault, your_key, mine_doc, _ = stocked
    found = await search.search(session, my_vault.id, "brackenridge", my_key)
    assert [hit.document_id for hit in found.hits] == [mine_doc]

    theirs = await search.search(session, your_vault.id, "brackenridge", your_key)
    assert theirs.total == 1
    assert theirs.hits[0].document_id != mine_doc


async def test_the_wrong_key_returns_nothing_rather_than_raising(session, stocked):
    """A caller holding the wrong key gets an empty result and a log line, not
    a 500 — and above all not somebody else's text."""
    my_vault, _my_key, _your_vault, your_key, *_ = stocked
    found = await search.search(session, my_vault.id, "brackenridge", your_key)
    assert found.total == 0
    assert found.hits == []


async def test_one_corrupt_page_does_not_take_the_search_down(session, stocked, caplog):
    """Invariant 8: it keeps working *and* it says so."""
    my_vault, my_key, _yv, _yk, _doc, item = stocked
    session.add(
        VaultPage(vault_item_id=item.id, page_number=3, sealed_text=b"not ciphertext")
    )
    await session.commit()

    with caplog.at_level("ERROR", logger="bindery.vault"):
        found = await search.search(session, my_vault.id, "brackenridge", my_key)
    assert found.pages_scanned == 3
    assert found.total == 1, "a corrupt neighbour swallowed a good hit"
    assert any(
        "did not decrypt" in record.getMessage() for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


async def test_a_query_with_nothing_in_it_scans_nothing(session, stocked):
    """Cheap, but the alternative is decrypting the entire vault to match ''."""
    my_vault, my_key, *_ = stocked
    for query in ("", "   ", "a"):
        found = await search.search(session, my_vault.id, query, my_key)
        assert found.total == 0
        assert found.pages_scanned == 0


async def test_it_reports_what_it_cost(session, stocked):
    """The scan is linear, so the numbers that predict when it stops being
    acceptable are returned rather than left to be discovered."""
    my_vault, my_key, *_ = stocked
    found = await search.search(session, my_vault.id, "brackenridge", my_key)
    assert found.pages_scanned == 2
    assert found.elapsed_ms >= 0
    assert found.slow is False


async def test_the_limit_caps_hits_without_lying_about_the_total(session, stocked):
    my_vault, my_key, _yv, _yk, _doc, item = stocked
    for number in range(4, 9):
        session.add(
            VaultPage(
                vault_item_id=item.id, page_number=number,
                sealed_text=crypto.encrypt(f"{PHRASE} again".encode(), my_key),
            )
        )
    await session.commit()

    found = await search.search(session, my_vault.id, "brackenridge", my_key, limit=2)
    assert len(found.hits) == 2
    assert found.total == 6
