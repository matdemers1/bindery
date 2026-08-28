"""T-1.11 — serving the archive's bytes. Authenticated and scoped (REQ-106).

There are no static routes: a leaked URL is worth nothing without a session.
"""

import json
import uuid

import pytest

from api.artifacts import derived_for, relative_to_data
from api.db.enums import IngestSource, SourceFileState
from api.db.models import Page, SourceFile
from api.storage.blobs import blob_path

WEBP = (
    b"RIFF$\x00\x00\x00WEBPVP8 \x18\x00\x00\x000\x01\x00\x9d\x01*\x01\x00\x01\x00"
    b"\x02\x007\x25\xa4\x00\x03p\x00\xfe\xfb\xfd\x50\x00"
)


@pytest.fixture
async def stored_file(session, signed_in):
    """A processed one-page file with its artifacts actually on disk."""
    _, library = await signed_in()
    sha = uuid.uuid4().hex * 2
    source_file = SourceFile(
        library_id=library.id,
        sha256=sha,
        byte_size=len(WEBP),
        original_filename="Utility Bill.pdf",
        ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1,
        state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    await session.flush()

    paths = derived_for(sha)
    paths.mkdirs()
    paths.page_render(1).write_bytes(WEBP)
    paths.page_thumb(1).write_bytes(WEBP)
    paths.normalized_pdf.write_bytes(b"%PDF-1.7\n%%EOF\n")
    paths.word_boxes.write_text(
        json.dumps(
            {
                "generator": "test",
                "pages": [
                    {
                        "number": 1,
                        "width": 612.0,
                        "height": 792.0,
                        "lines": [
                            {"words": [{"x0": 10, "y0": 20, "x1": 60, "y1": 32, "t": "Water"}]}
                        ],
                    }
                ],
            }
        )
    )

    session.add(
        Page(
            source_file_id=source_file.id,
            page_number=1,
            text="Water utility statement",
            render_path=relative_to_data(paths.page_render(1)),
            thumb_path=relative_to_data(paths.page_thumb(1)),
            word_boxes_path=relative_to_data(paths.word_boxes),
        )
    )
    await session.commit()
    return source_file


async def test_render_requires_a_session(client, stored_file) -> None:
    """REQ-106 — the direct URL is worthless on its own."""
    await client.post("/api/auth/logout")
    response = await client.get(f"/api/files/{stored_file.id}/pages/1/render")
    assert response.status_code == 401


async def test_render_is_served_to_the_owner(client, stored_file) -> None:
    response = await client.get(f"/api/files/{stored_file.id}/pages/1/render")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.content == WEBP
    # Content-addressed, so the bytes behind a URL can never change.
    assert "immutable" in response.headers["cache-control"]
    assert "private" in response.headers["cache-control"]


async def test_thumb_is_served(client, stored_file) -> None:
    response = await client.get(f"/api/files/{stored_file.id}/pages/1/thumb")
    assert response.status_code == 200
    assert response.content == WEBP


async def test_word_boxes_return_only_the_requested_page(client, stored_file) -> None:
    body = (await client.get(f"/api/files/{stored_file.id}/pages/1/boxes")).json()
    assert body["number"] == 1
    assert body["width"] == 612.0
    assert body["lines"][0]["words"][0]["t"] == "Water"


async def test_pdf_prefers_the_normalized_artifact(client, stored_file) -> None:
    response = await client.get(f"/api/files/{stored_file.id}/pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")


async def test_pdf_falls_back_to_the_original_when_not_yet_normalized(
    client, session, signed_in
) -> None:
    """A file still in the queue is still worth handing over."""
    _, library = await signed_in()
    sha = uuid.uuid4().hex * 2
    source_file = SourceFile(
        library_id=library.id,
        sha256=sha,
        byte_size=9,
        original_filename="raw.pdf",
        ingest_source=IngestSource.WEB_UPLOAD,
        state=SourceFileState.RECEIVED,
    )
    session.add(source_file)
    await session.commit()

    original = blob_path(sha)
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"%PDF-orig")

    response = await client.get(f"/api/files/{source_file.id}/pdf")
    assert response.status_code == 200
    assert response.content == b"%PDF-orig"


async def test_a_missing_blob_is_reported_as_an_integrity_problem(
    client, session, signed_in
) -> None:
    """404 would read as 'no such file'. This file exists; its bytes do not."""
    _, library = await signed_in()
    source_file = SourceFile(
        library_id=library.id,
        sha256=uuid.uuid4().hex * 2,
        byte_size=1,
        ingest_source=IngestSource.WEB_UPLOAD,
        state=SourceFileState.RECEIVED,
    )
    session.add(source_file)
    await session.commit()

    response = await client.get(f"/api/files/{source_file.id}/pdf")
    assert response.status_code == 410
    assert "integrity" in response.json()["detail"]


async def test_another_library_s_file_is_indistinguishable_from_a_missing_one(
    client, stored_file, signed_in
) -> None:
    """Whether a document exists in a library you cannot see is itself information."""
    await client.post("/api/auth/logout")
    await signed_in(library_name="Someone Else")

    for path in ("", "/pdf", "/pages/1/render", "/pages/1/thumb", "/pages/1/boxes"):
        response = await client.get(f"/api/files/{stored_file.id}{path}")
        assert response.status_code == 404, path


async def test_file_detail_lists_pages(client, stored_file) -> None:
    body = (await client.get(f"/api/files/{stored_file.id}")).json()
    assert body["source_file"]["original_filename"] == "Utility Bill.pdf"
    assert [page["page_number"] for page in body["pages"]] == [1]


async def test_a_stored_path_cannot_escape_the_data_root(session, signed_in) -> None:
    from api.artifacts import resolve_in_data

    with pytest.raises(ValueError):
        resolve_in_data("../../etc/passwd")
