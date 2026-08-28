"""OCR escalation: the page ocrmypdf declines to touch (T-1.4).

A page whose content is vector paths rather than an image makes ocrmypdf skip
it — *"no images - skipping all processing on this page to avoid losing
detail"* — and **exit 0**. The file then arrives fully processed, marked
`processed`, with no text and nothing reported anywhere.

For an archive that is the worst available outcome: not a failure you can see
and retry, but a document that is silently unfindable forever. It happened to a
real scanned authorization form, which produced 0 characters on ingest and 259
words once forced.

These tests are cheap and run in the default suite, because the input that
triggers the real thing is awkward to synthesise and the argv is the part that
silently rots.
"""

from pathlib import Path

from worker.stages.normalize import _ocr_argv, _word_count


def test_word_count_reads_the_extracted_structure() -> None:
    assert _word_count({"pages": []}) == 0
    assert _word_count({"pages": [{"lines": []}]}) == 0
    assert _word_count({"pages": [{"lines": [{"words": []}]}]}) == 0
    assert (
        _word_count(
            {
                "pages": [
                    {"lines": [{"words": [{"t": "a"}, {"t": "b"}]}]},
                    {"lines": [{"words": [{"t": "c"}]}]},
                ]
            }
        )
        == 3
    )


def test_force_ocr_replaces_skip_text_rather_than_joining_it() -> None:
    """The two flags are mutually exclusive; passing both is an ocrmypdf error."""
    normal = _ocr_argv(
        Path("in.pdf"), Path("out.pdf"), Path("side.txt"), pdfa=True, image=False
    )
    forced = _ocr_argv(
        Path("in.pdf"), Path("out.pdf"), Path("side.txt"), pdfa=True, image=False, force=True
    )

    assert "--skip-text" in normal and "--force-ocr" not in normal
    assert "--force-ocr" in forced and "--skip-text" not in forced


def test_the_escalation_keeps_everything_else_identical() -> None:
    """Only the one flag differs. A forced pass that also changed the language
    or dropped the sidecar would be a different operation wearing the same name."""
    common = dict(pdfa=True, image=True)
    normal = _ocr_argv(Path("i"), Path("o"), Path("s"), **common)
    forced = _ocr_argv(Path("i"), Path("o"), Path("s"), **common, force=True)

    assert [a for a in normal if a != "--skip-text"] == [
        a for a in forced if a != "--force-ocr"
    ]


# --------------------------------------------------------------------------
# Seeing what OCR actually read
# --------------------------------------------------------------------------


import uuid  # noqa: E402

import pytest  # noqa: E402

from api.db.enums import IngestSource, SourceFileState  # noqa: E402
from api.db.models import Page, SourceFile  # noqa: E402


@pytest.fixture
async def scanned(session, signed_in):
    _, library = await signed_in()
    source_file = SourceFile(
        library_id=library.id, sha256=uuid.uuid4().hex * 2, byte_size=100,
        original_filename="handwritten.pdf", ingest_source=IngestSource.WATCHED_FOLDER,
        page_count=3, state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    await session.flush()
    session.add_all([
        Page(source_file_id=source_file.id, page_number=1,
             text="Authorization to Contact\nand Request Personal Information"),
        # The case that matters: OCR read something, badly.
        Page(source_file_id=source_file.id, page_number=2, text="Matthevv  Dernevs"),
        Page(source_file_id=source_file.id, page_number=3, text="   "),
    ])
    await session.commit()
    return library, source_file


async def test_the_ocr_text_is_returned_verbatim(client, scanned) -> None:
    """Not cleaned up and not summarised.

    The whole point is comparing what the machine read against what is on the
    page — "Matthevv Dernevs" is the answer to "why does searching my own name
    find nothing", and tidying it away would destroy the only evidence.
    """
    _library, source_file = scanned
    response = await client.get(f"/api/files/{source_file.id}/text")
    assert response.status_code == 200, response.text
    body = response.json()

    assert [page["page_number"] for page in body["pages"]] == [1, 2, 3]
    assert body["pages"][1]["text"] == "Matthevv  Dernevs"
    # Line breaks survive: the layout is part of what you are checking.
    assert "\n" in body["pages"][0]["text"]


async def test_a_page_with_only_whitespace_counts_as_empty(client, scanned) -> None:
    _library, source_file = scanned
    body = (await client.get(f"/api/files/{source_file.id}/text")).json()

    assert body["pages"][2]["characters"] == 0
    assert body["empty_pages"] == 1
    assert body["characters"] > 0, "the file as a whole did yield text"


async def test_ocr_text_is_scoped_to_the_caller(client, session, scanned, user_factory) -> None:
    _user, elsewhere = await user_factory()
    stranger = SourceFile(
        library_id=elsewhere.id, sha256=uuid.uuid4().hex * 2, byte_size=10,
        original_filename="theirs.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1, state=SourceFileState.PROCESSED,
    )
    session.add(stranger)
    await session.flush()
    session.add(Page(source_file_id=stranger.id, page_number=1, text="private matters"))
    await session.commit()

    response = await client.get(f"/api/files/{stranger.id}/text")
    assert response.status_code == 404
    assert "private matters" not in response.text
