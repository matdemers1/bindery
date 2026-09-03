"""What the upload door refuses (CR-120).

Three separate holes, all at `POST /api/upload`:

- **No type check.** The importer walks past an unsupported suffix; this door
  took anything and handed it to OCR and to headless LibreOffice, a conversion
  toolchain reachable with attacker-chosen bytes. The two doors have to agree
  on what the archive accepts, and `walker.SUPPORTED` is the one definition.
- **A conditional quota check.** `if declared:` — and the declared size on a
  multipart part is whatever the client chose to send, so a falsy one skipped
  the account's storage limit entirely.
- **No ceiling of its own.** nginx caps a body at 512M; nothing did inside the
  application, and nothing counted the bytes that actually arrived.

The failure mode all three share is `api/quota.py`'s: filling the one disk pool
stops OCR, backups and Postgres for every household, and content-addressed
storage never deletes, so the space is not recoverable through the application.
"""

import uuid

import pytest

from api import quota
from api.backlog import walker
from api.db.enums import IngestSource
from api.db.models import SourceFile

PDF = b"%PDF-1.7\n% a small but valid-enough stand-in\n%%EOF\n"


def _args(library_id, content: bytes = PDF, name: str = "scan.pdf"):
    return {
        "data": {"library_id": str(library_id)},
        "files": {"file": (name, content, "application/pdf")},
    }


async def _hold(session, library, size: int) -> None:
    session.add(
        SourceFile(
            library_id=library.id,
            sha256=(uuid.uuid4().hex * 2)[:64],
            byte_size=size,
            original_filename="already-here.pdf",
            ingest_source=IngestSource.WEB_UPLOAD,
        )
    )
    await session.commit()


# --------------------------------------------------------------------------
# The suffix allowlist
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["payload.exe", "keys.zip", "notes.one", "noextension"])
async def test_a_suffix_the_importer_would_walk_past_is_refused(client, signed_in, name):
    _, library = await signed_in()
    response = await client.post(
        "/api/upload", **_args(library.id, PDF + name.encode(), name)
    )
    assert response.status_code == 415, response.text


async def test_the_two_doors_agree_on_what_the_archive_accepts(client, signed_in):
    """One definition — `walker.SUPPORTED` — not two lists that drift."""
    _, library = await signed_in()
    for name in ("scan.pdf", "photo.JPG", "statement.xlsx", "page.mht"):
        assert name[name.rfind(".") :].lower() in walker.SUPPORTED
        response = await client.post(
            "/api/upload", **_args(library.id, PDF + name.encode(), name)
        )
        assert response.status_code in (200, 201), (name, response.text)


# --------------------------------------------------------------------------
# The quota, whatever the client declared
# --------------------------------------------------------------------------


async def test_an_account_over_its_limit_cannot_upload_even_nothing(
    client, session, signed_in
):
    """The `if declared:` hole, in the one case that distinguishes it.

    A zero-byte part declares a size of 0, which is falsy — so the old check
    was skipped entirely and the upload went straight to the blob store. An
    account whose quota was lowered under what it already holds is over its
    limit, and the honest answer to anything more is 413.
    """
    user, library = await signed_in()
    await _hold(session, library, 4_000)
    # Lowered under what the account already holds, which is what an
    # administrator does and what leaves an account genuinely over its limit.
    user.storage_quota_bytes = 1_000
    await session.commit()

    response = await client.post("/api/upload", **_args(library.id, b"", "empty.pdf"))
    assert response.status_code == 413, response.text
    assert "storage limit" in response.json()["detail"]


async def test_a_declared_size_past_the_limit_is_refused_before_the_bytes_arrive(
    client, session, signed_in
):
    """The declaration is still good for one thing: refusing early, before a
    gigabyte crosses the wire."""
    user, library = await signed_in()
    await _hold(session, library, 900)
    user.storage_quota_bytes = 1_000
    await session.commit()

    response = await client.post(
        "/api/upload", **_args(library.id, b"x" * 5_000, "big.pdf")
    )
    assert response.status_code == 413, response.text


# --------------------------------------------------------------------------
# The ceiling on the bytes that actually arrive
# --------------------------------------------------------------------------


async def test_the_stream_is_cut_off_at_the_api_s_own_ceiling(
    client, signed_in, monkeypatch
):
    """A limit inside the application, not only in nginx.

    Read from `quota.MAX_UPLOAD_BYTES` at call time, so this can lower it
    rather than posting half a gigabyte.
    """
    _, library = await signed_in()
    monkeypatch.setattr(quota, "MAX_UPLOAD_BYTES", 64)

    response = await client.post(
        "/api/upload", **_args(library.id, b"y" * 4_096, "over-the-ceiling.pdf")
    )
    assert response.status_code == 413, response.text
    assert "refused part-way through" in response.json()["detail"]


async def test_capped_refuses_during_the_stream_not_after():
    """`store_stream` moves the temp file into the content-addressed store when
    the stream ends, and content-addressed storage never deletes — so the
    refusal has to happen while the bytes are still in flight."""

    async def chunks():
        for _ in range(10):
            yield b"z" * 100

    seen = 0
    with pytest.raises(quota.TooLarge):
        async for chunk in quota.capped(chunks(), 250):
            seen += len(chunk)
    assert seen < 1_000, "the whole stream was consumed before the refusal"
