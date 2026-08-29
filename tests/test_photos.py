"""The photo wall.

An image the OCR could not read carries no text, so it is invisible to search
and indistinguishable, in a list of filenames, from every other row. Shown as a
picture it is recognisable instantly — which is the whole reason this view
exists, and the reason the `undescribed` filter is the part worth testing: it
is what turns "everything" into "the ones that need a description".
"""

import uuid

import pytest

from api.db.enums import IngestSource, SourceFileState
from api.db.models import Document, SourceFile


async def _image(session, library, filename: str, *, title=None, summary=None):
    source_file = SourceFile(
        library_id=library.id,
        sha256=uuid.uuid4().hex * 2,
        byte_size=2048,
        original_filename=filename,
        ingest_source=IngestSource.WATCHED_FOLDER,
        page_count=1,
        state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    await session.flush()
    document = Document(
        library_id=library.id,
        source_file_id=source_file.id,
        page_start=1,
        page_end=1,
        title=title,
        summary=summary,
    )
    session.add(document)
    await session.flush()
    return source_file, document


@pytest.fixture
async def wall(session, signed_in):
    _user, library = await signed_in()
    described = await _image(
        session, library, "hangar.jpg", title="Hangar", summary="A hangar at dusk."
    )
    silent = await _image(session, library, "IMG_4417.HEIC")
    # A PDF is not a photograph, however much text it has.
    await _image(session, library, "orders.pdf", title="Orders", summary="Orders.")
    await session.commit()
    return library, described, silent


async def test_the_wall_holds_images_and_nothing_else(client, wall) -> None:
    body = (await client.get("/api/photos")).json()

    names = {photo["original_filename"] for photo in body["photos"]}
    assert names == {"hangar.jpg", "IMG_4417.HEIC"}
    assert body["total"] == 2


async def test_undescribed_finds_the_ones_nothing_has_said_anything_about(
    client, wall
) -> None:
    body = (await client.get("/api/photos", params={"undescribed": "true"})).json()

    assert [photo["original_filename"] for photo in body["photos"]] == ["IMG_4417.HEIC"]
    assert body["photos"][0]["described"] is False


async def test_described_needs_both_a_title_and_a_summary(client, wall) -> None:
    """A title alone is a filename with better manners, not a description."""
    body = (await client.get("/api/photos")).json()
    by_name = {photo["original_filename"]: photo for photo in body["photos"]}

    assert by_name["hangar.jpg"]["described"] is True
    assert by_name["IMG_4417.HEIC"]["described"] is False


async def test_search_matches_the_filename_as_well_as_the_title(client, wall) -> None:
    """You remember `IMG_4417` far more often than you remember what is in it."""
    body = (await client.get("/api/photos", params={"q": "4417"})).json()

    assert [photo["original_filename"] for photo in body["photos"]] == ["IMG_4417.HEIC"]


async def test_the_wall_does_not_leak_another_library(
    client, session, signed_in, user_factory
) -> None:
    await signed_in()
    _user, elsewhere = await user_factory()
    await _image(session, elsewhere, "not-yours.png", title="No", summary="No.")
    await session.commit()

    body = (await client.get("/api/photos")).json()

    assert all(p["original_filename"] != "not-yours.png" for p in body["photos"])
