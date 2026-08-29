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
from api.db.models import Classification, Document, Page, SourceFile


async def _image(
    session, library, filename: str, *, title=None, summary=None, text="", looked_at=0
):
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
    session.add(
        Page(
            source_file_id=source_file.id,
            page_number=1,
            text=text,
            render_path="derived/x/pages/0001.webp",
            thumb_path="derived/x/thumbs/0001.webp",
        )
    )
    await session.flush()
    if looked_at:
        session.add(
            Classification(
                document_id=document.id,
                model="claude-sonnet-5",
                prompt_version="v1",
                page_images_sent=looked_at,
            )
        )
    await session.flush()
    return source_file, document


@pytest.fixture
async def wall(session, signed_in):
    _user, library = await signed_in()
    described = await _image(
        session,
        library,
        "hangar.jpg",
        title="Hangar",
        summary="A hangar at dusk.",
        text="MAINTENANCE HANGAR 4 — AUTHORISED PERSONNEL ONLY. Posted 14 March 2019.",
    )
    # Classification ran on this one too and produced a confident title from an
    # empty page. That is the case the filter has to catch.
    silent = await _image(
        session,
        library,
        "IMG_4417.HEIC",
        title="Unreadable Scan",
        summary="The page contains no extractable text.",
    )
    # A PDF is not a photograph, however much text it has.
    await _image(session, library, "orders.pdf", title="Orders", summary="Orders.")
    await session.commit()
    return library, described, silent


async def test_the_wall_holds_images_and_nothing_else(client, wall) -> None:
    body = (await client.get("/api/photos")).json()

    names = {photo["original_filename"] for photo in body["photos"]}
    assert names == {"hangar.jpg", "IMG_4417.HEIC"}
    assert body["total"] == 2


async def test_undescribed_finds_the_pictures_nothing_could_be_read_from(
    client, wall
) -> None:
    """The filter asks what was readable, not whether a column is null.

    Every one of the 182 images in the real archive had a title. The titles
    were summaries of an empty string — "Unreadable Scan", "Blank or
    Unreadable Scan" — so a null check found nothing at all.
    """
    body = (await client.get("/api/photos", params={"undescribed": "true"})).json()

    assert [photo["original_filename"] for photo in body["photos"]] == ["IMG_4417.HEIC"]
    assert body["photos"][0]["described"] is False
    assert body["photos"][0]["text_chars"] == 0
    # It has a title. That is precisely why the title cannot be the test.
    assert body["photos"][0]["title"] == "Unreadable Scan"


async def test_described_requires_something_to_have_been_read(client, wall) -> None:
    """A title written from an empty page describes nothing."""
    body = (await client.get("/api/photos")).json()
    by_name = {photo["original_filename"]: photo for photo in body["photos"]}

    assert by_name["hangar.jpg"]["described"] is True
    assert by_name["hangar.jpg"]["text_chars"] > 40
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
    await _image(session, elsewhere, "not-yours.png", title="No", summary="No.", text="No")
    await session.commit()

    body = (await client.get("/api/photos")).json()

    assert all(p["original_filename"] != "not-yours.png" for p in body["photos"])


async def test_a_photograph_something_has_looked_at_is_described(
    client, session, signed_in
) -> None:
    """OCR reads nothing off a photograph before *or* after it is described.

    Keying the filter on text alone meant the wall went on offering to describe
    the same 146 images forever, at real cost per press. The question it has to
    ask is whether anything ever looked at them.
    """
    _user, library = await signed_in()
    await _image(
        session, library, "patch.png",
        title="805th Combat Training Squadron - Unit Patch",
        summary="A squadron patch with a lightning bolt.",
        looked_at=1,
    )
    await _image(session, library, "never-seen.png")
    await session.commit()

    body = (await client.get("/api/photos", params={"undescribed": "true"})).json()

    assert [p["original_filename"] for p in body["photos"]] == ["never-seen.png"]

    everything = (await client.get("/api/photos")).json()
    by_name = {p["original_filename"]: p for p in everything["photos"]}
    assert by_name["patch.png"]["described"] is True
    assert by_name["patch.png"]["text_chars"] == 0, "still nothing readable on it"
