"""T-0.8 — the minimal ingest path, and the scoping around it."""

import hashlib

import sqlalchemy as sa

from api.db.enums import LibraryKind, MembershipRole
from api.db.models import AuditEvent, Library
from api.storage.blobs import blob_path

PDF = b"%PDF-1.7\n% a small but valid-enough stand-in\n%%EOF\n"


def _upload_args(library_id, content: bytes = PDF, name: str = "scan.pdf"):
    return {
        "data": {"library_id": str(library_id)},
        "files": {"file": (name, content, "application/pdf")},
    }


async def test_upload_requires_authentication(client) -> None:
    library_id = "00000000-0000-0000-0000-000000000000"
    response = await client.post("/api/upload", **_upload_args(library_id))
    assert response.status_code == 401


async def test_upload_stores_a_content_addressed_blob(client, signed_in) -> None:
    _, library = await signed_in()

    response = await client.post("/api/upload", **_upload_args(library.id))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["duplicate"] is False
    stored = body["source_file"]
    assert stored["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert stored["byte_size"] == len(PDF)
    assert stored["state"] == "received"

    path = blob_path(stored["sha256"])
    assert path.is_file()
    assert path.read_bytes() == PDF
    # Write-once, enforced by the filesystem (invariant 1).
    assert path.stat().st_mode & 0o222 == 0


async def test_identical_bytes_are_not_stored_twice(client, signed_in) -> None:
    _, library = await signed_in()
    content = PDF + b"unique-for-this-test\n"

    first = await client.post("/api/upload", **_upload_args(library.id, content))
    second = await client.post("/api/upload", **_upload_args(library.id, content, "again.pdf"))

    assert first.status_code == 201
    assert first.json()["duplicate"] is False
    # Nothing was created the second time, so it is not a 201.
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["source_file"]["id"] == first.json()["source_file"]["id"]


async def test_upload_writes_an_audit_event(client, signed_in, session) -> None:
    user, library = await signed_in()
    content = PDF + b"audited\n"

    response = await client.post("/api/upload", **_upload_args(library.id, content))
    source_file_id = response.json()["source_file"]["id"]

    event = (
        await session.execute(
            sa.select(AuditEvent).where(AuditEvent.entity_id == source_file_id)
        )
    ).scalar_one()
    assert event.action == "ingest"
    assert event.actor_id == user.id
    assert event.after["sha256"] == hashlib.sha256(content).hexdigest()


async def test_upload_into_someone_elses_library_is_refused(client, signed_in, session) -> None:
    await signed_in()
    other = Library(name="Not Yours", kind=LibraryKind.PERSONAL)
    session.add(other)
    await session.commit()

    response = await client.post("/api/upload", **_upload_args(other.id))

    assert response.status_code == 403


async def test_readers_cannot_upload(client, signed_in) -> None:
    """Read access to a library is not write access to it."""
    _, library = await signed_in(role=MembershipRole.READER, library_name="Read Only")
    response = await client.post("/api/upload", **_upload_args(library.id))
    assert response.status_code == 403


async def test_source_file_list_is_library_scoped(client, signed_in) -> None:
    """The seam Phase 7's leak suite will lean on: one query, one filter."""
    _, mine = await signed_in()
    await client.post("/api/upload", **_upload_args(mine.id, PDF + b"mine\n"))
    assert len((await client.get("/api/source-files")).json()) == 1

    await client.post("/api/auth/logout")
    await signed_in(library_name="Stranger")

    visible = (await client.get("/api/source-files")).json()

    assert visible == [], "a user saw a source file from a library they are not a member of"


async def test_documents_list_is_empty_until_segmentation(client, signed_in) -> None:
    await signed_in()
    response = await client.get("/api/documents")
    assert response.status_code == 200
    assert response.json() == []


async def test_libraries_lists_only_the_callers_memberships(client, signed_in) -> None:
    _, library = await signed_in()
    rows = (await client.get("/api/libraries")).json()
    assert [row["id"] for row in rows] == [str(library.id)]
