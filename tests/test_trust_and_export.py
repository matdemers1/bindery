"""Phase 6 — trust, export and resilience (T-6.1 to T-6.8).

The verification steps from the phase plan, in the plan's own order:

1. Corrupt a blob deliberately → the integrity check finds it (REQ-095)
2. The full export is navigable **without Bindery** (REQ-093)
3. Delete the mirror tree → it rebuilds; `_bundles/` lists page ranges (REQ-094, REQ-043)
4. Offsite copies are verified encrypted (REQ-096)
5. Restore drill (REQ-097) — the shell script, exercised here for its guarantees
6. Vital documents surface without searching (REQ-091)
7. The go-bag is produced and decryptable (REQ-092)
8. The audit log filters by document, actor and time (REQ-069)
"""

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from api.db.enums import ActorType, IngestSource, Sensitivity, SourceFileState
from api.db.models import (
    AuditEvent,
    Correspondent,
    Document,
    DocumentTag,
    KnownForm,
    Page,
    SourceFile,
    Tag,
)
from api.export import archive_export, backup, integrity, mirror
from api.export.tree import deduplicate, document_filename, document_folder, safe_component
from api.storage.blobs import blob_path


def _write_blob(content: bytes) -> str:
    """Put a real file in the blob store, the way ingest would."""
    sha = hashlib.sha256(content).hexdigest()
    path = blob_path(sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.chmod(path, 0o444)
    return sha


@pytest.fixture
async def archive(session, signed_in, tmp_path):
    """A small but structurally complete archive: a whole file and a bundle."""
    _, library = await signed_in()

    # One whole-file document, marked vital — the DD-214 the drill looks for.
    dd214_bytes = b"%PDF-1.4 DD FORM 214 CERTIFICATE OF RELEASE " + uuid.uuid4().bytes
    dd214_sha = _write_blob(dd214_bytes)
    whole = SourceFile(
        library_id=library.id, sha256=dd214_sha, byte_size=len(dd214_bytes),
        original_filename="scan-dd214.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1, state=SourceFileState.PROCESSED,
    )

    # One bundle: three documents inside a single twelve-page scan.
    bundle_bytes = b"%PDF-1.4 mixed household scan " + uuid.uuid4().bytes
    bundle_sha = _write_blob(bundle_bytes)
    bundle = SourceFile(
        library_id=library.id, sha256=bundle_sha, byte_size=len(bundle_bytes),
        original_filename="scan_0012.pdf", ingest_source=IngestSource.WATCHED_FOLDER,
        page_count=12, state=SourceFileState.PROCESSED,
    )
    session.add_all([whole, bundle])
    await session.flush()

    va = Correspondent(
        library_id=library.id, name="U.S. Dept of Veterans Affairs",
        slug=f"va-{uuid.uuid4().hex[:6]}",
    )
    form = KnownForm(
        code=f"DD-214-{uuid.uuid4().hex[:4]}", name="Certificate of Release or Discharge",
        match_rules={}, field_extractors={},
    )
    tag = Tag(library_id=library.id, name="military", slug=f"military-{uuid.uuid4().hex[:6]}")
    session.add_all([va, form, tag])
    await session.flush()

    for n in range(1, 13):
        session.add(Page(source_file_id=bundle.id, page_number=n, text=f"bundle page {n}"))
    session.add(Page(source_file_id=whole.id, page_number=1, text="DD FORM 214 discharge"))

    dd214 = Document(
        library_id=library.id, source_file_id=whole.id, page_start=1, page_end=1,
        title="Certificate of Release or Discharge", correspondent_id=va.id,
        known_form_id=form.id, sensitivity=Sensitivity.VITAL,
        document_date=datetime(2009, 6, 14, tzinfo=UTC).date(),
    )
    inside = [
        Document(
            library_id=library.id, source_file_id=bundle.id,
            page_start=start, page_end=end, title=title, correspondent_id=va.id,
            document_date=datetime(2019, 4, 12, tzinfo=UTC).date(),
        )
        for start, end, title in [
            (1, 3, "Award Letter"), (4, 8, "Rating Decision"), (9, 12, "Cover Sheet"),
        ]
    ]
    session.add_all([dd214, *inside])
    await session.flush()
    session.add(DocumentTag(document_id=dd214.id, tag_id=tag.id, source="human"))
    await session.commit()

    return {
        "library": library, "dd214": dd214, "dd214_sha": dd214_sha,
        "bundle_sha": bundle_sha, "inside": inside, "tmp": tmp_path,
    }


# --------------------------------------------------------------------------
# 1. Integrity (T-6.5, REQ-095)
# --------------------------------------------------------------------------


async def test_deliberately_corrupting_a_blob_is_detected(session, archive) -> None:
    """The failure this phase exists to catch, forced to happen.

    Bit rot is silent for years. If this test did not exist, nothing in the
    system would ever notice a decayed original until the day it was needed.
    """
    report = await integrity.check(session, library_ids=[archive["library"].id])
    assert report.healthy
    assert report.corrupt == []

    path = blob_path(archive["dd214_sha"])
    os.chmod(path, 0o644)
    path.write_bytes(b"%PDF-1.4 corrupted by bit rot")

    after = await integrity.check(session, library_ids=[archive["library"].id])
    assert not after.healthy
    assert [entry["sha256"] for entry in after.corrupt] == [archive["dd214_sha"]]
    # It reports what the bytes actually hash to, not just that they differ —
    # that is what tells you whether a backup generation has the good copy.
    assert after.corrupt[0]["actual_sha256"] != archive["dd214_sha"]


async def test_a_missing_original_is_reported_not_ignored(session, archive) -> None:
    path = blob_path(archive["bundle_sha"])
    os.chmod(path, 0o644)
    path.unlink()

    report = await integrity.check(session, library_ids=[archive["library"].id])
    assert not report.healthy
    assert [entry["sha256"] for entry in report.missing] == [archive["bundle_sha"]]


async def test_orphan_blobs_are_listed_but_never_removed(session, archive) -> None:
    """REQ-090 has no exceptions, and an 'orphan' is also an interrupted ingest."""
    orphan_sha = _write_blob(b"unreferenced " + uuid.uuid4().bytes)

    report = await integrity.check(session)
    assert orphan_sha in report.orphans
    assert blob_path(orphan_sha).is_file(), "the check must not delete anything"


# --------------------------------------------------------------------------
# 2. Full export works without Bindery (T-6.3, REQ-093)
# --------------------------------------------------------------------------


async def test_full_export_is_a_folder_tree_of_untouched_originals(
    session, archive
) -> None:
    destination = archive["tmp"] / "export"
    result = await archive_export.full_export(
        session, [archive["library"].id], destination=destination
    )

    assert result.document_count == 4
    assert result.missing_blobs == []

    # The whole-file document is filed by who it came from and when.
    expected = destination / "U.S. Dept of Veterans Affairs" / "2009"
    filed = list(expected.glob("*.pdf"))
    assert len(filed) == 1, f"expected one filed original, got {filed}"
    assert filed[0].name.startswith("2009-06-14 - Certificate of Release")

    # Byte-for-byte. Nothing is re-encoded on the way out.
    assert (
        hashlib.sha256(filed[0].read_bytes()).hexdigest() == archive["dd214_sha"]
    ), "the exported original must be the original"


async def test_the_export_index_opens_in_a_browser_with_no_server(
    session, archive
) -> None:
    """REQ-093, stated precisely: no scripts, no fetches, no absolute paths.

    If the index needed a server, the export would only work while the thing it
    is insurance against is still running.
    """
    destination = archive["tmp"] / "export"
    await archive_export.full_export(
        session, [archive["library"].id], destination=destination
    )
    index = (destination / "index.html").read_text()

    assert "<script" not in index.lower()
    assert "http://" not in index and "https://" not in index
    assert "src=" not in index, "no external assets to fail to load"
    assert "Certificate of Release" in index

    # Every link resolves to a file that is actually there.
    hrefs = [part.split("'")[0] for part in index.split("href='")[1:]]
    assert hrefs
    for href in hrefs:
        assert not href.startswith("/"), "relative links survive being copied anywhere"
        assert (destination / href).exists(), f"dead link in the index: {href}"


async def test_a_bundle_exports_as_a_folder_with_a_page_index(session, archive) -> None:
    """REQ-043. A twelve-page scan holding three documents cannot become three
    files without splitting the original, so it becomes a folder that says what
    is on which page."""
    destination = archive["tmp"] / "export"
    await archive_export.full_export(
        session, [archive["library"].id], destination=destination
    )

    bundles = list((destination / "_bundles").iterdir())
    assert len(bundles) == 1
    folder = bundles[0]
    assert "12 pages" in folder.name

    index = (folder / "index.html").read_text()
    for title, pages in [
        ("Award Letter", "pp. 1–3"),
        ("Rating Decision", "pp. 4–8"),
        ("Cover Sheet", "pp. 9–12"),
    ]:
        assert title in index
        assert pages in index

    originals = [p for p in folder.iterdir() if p.suffix == ".pdf"]
    assert len(originals) == 1, "the bundle is copied once, not once per document"
    assert (
        hashlib.sha256(originals[0].read_bytes()).hexdigest() == archive["bundle_sha"]
    )


async def test_the_json_sidecar_carries_the_same_facts_as_the_page(
    session, archive
) -> None:
    destination = archive["tmp"] / "export"
    await archive_export.full_export(
        session, [archive["library"].id], destination=destination
    )
    payload = json.loads((destination / "documents.json").read_text())
    assert payload["document_count"] == 4
    dd214 = next(d for d in payload["documents"] if d["sensitivity"] == "vital")
    assert dd214["correspondent"] == "U.S. Dept of Veterans Affairs"
    assert dd214["tags"] == ["military"]
    assert (destination / dd214["path"]).is_file()


async def test_an_export_says_so_when_an_original_is_gone(session, archive) -> None:
    """Silently omitting a missing original would make the export a lie."""
    path = blob_path(archive["dd214_sha"])
    os.chmod(path, 0o644)
    path.unlink()

    destination = archive["tmp"] / "export"
    result = await archive_export.full_export(
        session, [archive["library"].id], destination=destination
    )
    assert result.missing_blobs == [archive["dd214_sha"]]
    payload = json.loads((destination / "documents.json").read_text())
    assert any(d.get("missing_original") for d in payload["documents"])


# --------------------------------------------------------------------------
# 3. Mirror tree (T-6.4, REQ-094)
# --------------------------------------------------------------------------


async def test_deleting_the_mirror_loses_nothing(session, archive) -> None:
    root = archive["tmp"] / "mirror"
    first = await mirror.rebuild(session, [archive["library"].id], root=root)
    assert first.bundles == 1
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))

    import shutil

    shutil.rmtree(root)
    assert not root.exists()

    second = await mirror.rebuild(session, [archive["library"].id], root=root)
    after = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    assert after == before
    assert second.missing == 0


async def test_mirror_entries_are_hardlinks_not_copies(session, archive) -> None:
    """A mirror of a 400GB archive should cost directory entries, not 400GB."""
    root = archive["tmp"] / "mirror"
    result = await mirror.rebuild(session, [archive["library"].id], root=root)
    if result.copied:
        pytest.skip("mirror is on a different filesystem from the blob store")

    filed = next((root / "U.S. Dept of Veterans Affairs" / "2009").glob("*.pdf"))
    assert filed.stat().st_ino == blob_path(archive["dd214_sha"]).stat().st_ino
    # Mode 0444 on the blob means the hardlink cannot be used to edit the
    # original, which is what makes sharing an inode safe here.
    assert filed.stat().st_mode & 0o222 == 0


async def test_rebuilding_removes_entries_for_superseded_documents(
    session, archive
) -> None:
    root = archive["tmp"] / "mirror"
    await mirror.rebuild(session, [archive["library"].id], root=root)
    assert (root / "U.S. Dept of Veterans Affairs" / "2009").exists()

    archive["dd214"].superseded_at = datetime.now(UTC)
    await session.commit()

    result = await mirror.rebuild(session, [archive["library"].id], root=root)
    assert result.removed >= 1
    assert not (root / "U.S. Dept of Veterans Affairs" / "2009").exists()
    # The derived entry went; the original did not.
    assert blob_path(archive["dd214_sha"]).is_file()


# --------------------------------------------------------------------------
# 4/5. Backup and restore (T-6.6, T-6.7, REQ-096, REQ-097)
# --------------------------------------------------------------------------


async def test_backup_refuses_to_run_over_a_failing_integrity_check(
    session, archive
) -> None:
    """A backup of known-corrupt data eventually rotates out the good copy."""
    path = blob_path(archive["dd214_sha"])
    os.chmod(path, 0o644)
    path.write_bytes(b"rot")

    report = await integrity.check(session, library_ids=[archive["library"].id])
    with pytest.raises(RuntimeError, match="refusing to back up"):
        backup.run_backup(report, destination=archive["tmp"] / "backup")

    assert not (archive["tmp"] / "backup" / "bindery.dump").exists()


async def test_offsite_copies_are_verified_encrypted(archive) -> None:
    """The failure that matters is an encryption step that silently no-opped."""
    source = archive["tmp"] / "generation"
    source.mkdir()
    (source / "bindery.dump").write_bytes(b"pretend pg_dump output")

    plaintext = archive["tmp"] / "plain.tar"
    plaintext.write_bytes(b"not encrypted at all")
    assert backup.verify_encrypted(plaintext) is False

    encrypted = archive["tmp"] / "offsite.enc"
    backup.encrypt_for_offsite(source, encrypted, "a-passphrase-long-enough-to-matter")
    assert backup.verify_encrypted(encrypted) is True
    assert b"pretend pg_dump output" not in encrypted.read_bytes()
    assert not encrypted.with_suffix(".tar").exists(), "the plaintext tar is cleaned up"


def test_a_short_offsite_passphrase_is_refused(tmp_path) -> None:
    source = tmp_path / "gen"
    source.mkdir()
    with pytest.raises(ValueError, match="at least 16"):
        backup.encrypt_for_offsite(source, tmp_path / "out.enc", "short")


def test_the_restore_drill_fails_loudly_when_it_cannot_find_the_document() -> None:
    """The drill's whole value is that it can fail.

    A drill that always passes is a ceremony. These are the assertions in
    scripts/restore-drill.sh that make it a test rather than a ritual.
    """
    script = Path(__file__).resolve().parent.parent / "scripts" / "restore-drill.sh"
    body = script.read_text()

    assert "DRILL FAILED" in body
    assert "exit 1" in body
    # It must verify the blobs, not just that pg_restore returned zero.
    assert "MISSING BLOB" in body
    # It must never point at the live database.
    assert "bindery-restore-drill" in body
    assert "--exit-on-error" in body


# --------------------------------------------------------------------------
# 6/7. Vital records and the go-bag (T-6.1, T-6.2, REQ-091, REQ-092)
# --------------------------------------------------------------------------


async def test_vital_documents_appear_without_searching(client, archive) -> None:
    response = await client.get("/api/vital")
    assert response.status_code == 200, response.text
    titles = [item["title"] for item in response.json()]
    assert "Certificate of Release or Discharge" in titles
    assert "Award Letter" not in titles, "only the vital tier is pinned"


async def test_the_go_bag_holds_only_vital_records_and_decrypts(
    session, archive
) -> None:
    pyzipper = pytest.importorskip("pyzipper")

    target = archive["tmp"] / "go-bag.zip"
    passphrase = "correct-horse-battery-staple"
    result = await archive_export.go_bag(
        session, [archive["library"].id], passphrase, destination=target
    )
    assert result.encrypted
    assert result.document_count == 1

    with pyzipper.AESZipFile(target) as zipped:
        # Encrypted for real: the right passphrase is required to read content.
        with pytest.raises(RuntimeError):
            zipped.read("documents.json")
        zipped.setpassword(passphrase.encode())
        payload = json.loads(zipped.read("documents.json"))
        assert [d["title"] for d in payload] == ["Certificate of Release or Discharge"]
        names = zipped.namelist()

    assert any(name.endswith(".pdf") for name in names)
    assert "index.html" in names, "the go-bag is browsable too"


async def test_a_weak_go_bag_passphrase_is_refused(session, archive) -> None:
    """It is going to leave the house on a USB stick."""
    with pytest.raises(ValueError, match="at least 12"):
        await archive_export.go_bag(session, [archive["library"].id], "hunter2")


# --------------------------------------------------------------------------
# 8. Audit log viewer (T-6.8, REQ-069)
# --------------------------------------------------------------------------


@pytest.fixture
async def audit_trail(session, archive):
    document = archive["dd214"]
    other = archive["inside"][0]
    session.add_all([
        AuditEvent(entity_type="document", entity_id=document.id, action="filed",
                   actor_type=ActorType.AI),
        AuditEvent(entity_type="document", entity_id=document.id, action="tag_added",
                   actor_type=ActorType.HUMAN),
        AuditEvent(entity_type="document", entity_id=other.id, action="filed",
                   actor_type=ActorType.RULE),
    ])
    await session.commit()
    return document, other


async def test_audit_log_filters_by_document(client, audit_trail) -> None:
    document, other = audit_trail
    response = await client.get("/api/audit", params={"entity_id": str(document.id)})
    assert response.status_code == 200, response.text
    events = response.json()["events"]
    assert events, "the document has history"
    assert {event["entity_id"] for event in events} == {str(document.id)}
    assert str(other.id) not in {event["entity_id"] for event in events}


async def test_audit_log_filters_by_actor_type(client, audit_trail) -> None:
    document, _ = audit_trail
    response = await client.get(
        "/api/audit", params={"entity_id": str(document.id), "actor_type": "ai"}
    )
    actions = [event["action"] for event in response.json()["events"]]
    assert actions == ["filed"], "the human tag_added event is filtered out"


async def test_audit_log_filters_by_time(client, session, audit_trail) -> None:
    document, _ = audit_trail
    future = datetime.now(UTC) + timedelta(hours=1)
    response = await client.get(
        "/api/audit", params={"entity_id": str(document.id), "since": future.isoformat()}
    )
    assert response.json()["events"] == []


async def test_audit_log_is_ordered_by_sequence_not_timestamp(
    client, session, audit_trail
) -> None:
    """Events written in one transaction share a `created_at`.

    Ordering by timestamp would make the history of a single bulk edit
    arbitrary; `sequence` is the only total order there is.
    """
    document, _ = audit_trail
    response = await client.get("/api/audit", params={"entity_id": str(document.id)})
    sequences = [event["sequence"] for event in response.json()["events"]]
    assert sequences == sorted(sequences, reverse=True)
    assert len(set(sequences)) == len(sequences)


async def test_audit_log_pages_without_skipping_or_repeating(
    client, session, archive
) -> None:
    document = archive["dd214"]
    session.add_all([
        AuditEvent(entity_type="document", entity_id=document.id,
                   action=f"step-{n}", actor_type=ActorType.SYSTEM)
        for n in range(10)
    ])
    await session.commit()

    seen: list[int] = []
    cursor = None
    for _ in range(5):
        params = {"entity_id": str(document.id), "limit": 4}
        if cursor:
            params["before_sequence"] = cursor
        page = (await client.get("/api/audit", params=params)).json()
        seen.extend(event["sequence"] for event in page["events"])
        cursor = page["next_before_sequence"]
        if cursor is None:
            break

    assert len(seen) == len(set(seen)), "keyset paging must not repeat a row"
    assert seen == sorted(seen, reverse=True)


# --------------------------------------------------------------------------
# Path safety — the export has to survive being copied onto a Windows machine
# --------------------------------------------------------------------------


def test_path_components_survive_hostile_filenames() -> None:
    assert safe_component('VA: Rating/Decision*') == "VA- Rating-Decision-"
    assert safe_component("CON") == "_CON"
    assert safe_component("   ...   ") == "Untitled"
    assert safe_component(None) == "Untitled"
    assert len(safe_component("x" * 500)) <= 100


def test_two_documents_with_the_same_name_both_survive() -> None:
    """Overwriting one award letter with another is exactly the quiet data loss
    this system exists to prevent."""
    taken: set[str] = set()
    names = [deduplicate("Award Letter.pdf", taken) for _ in range(3)]
    assert names == [
        "Award Letter.pdf", "Award Letter (2).pdf", "Award Letter (3).pdf"
    ]


def test_undated_and_unfiled_documents_get_honest_placeholders() -> None:
    assert document_folder(None, None) == ("Unfiled", "Undated")
    assert document_filename(title=None, document_date=None, extension=".pdf") == (
        "Untitled.pdf"
    )


# --------------------------------------------------------------------------
# Browsing the tree in the app — "what is in here, and how did it get in?"
# --------------------------------------------------------------------------


async def test_the_top_of_the_tree_lists_folders_not_a_flat_dump(
    client, archive
) -> None:
    response = await client.get("/api/tree")
    assert response.status_code == 200, response.text
    nodes = response.json()["nodes"]

    names = {node["name"]: node for node in nodes}
    assert "U.S. Dept of Veterans Affairs" in names
    assert names["U.S. Dept of Veterans Affairs"]["kind"] == "folder"
    assert names["_bundles"]["kind"] == "folder"
    # Four documents live under these two folders, and none of them leak to the
    # top level: browsing is a walk, not a dump.
    assert all(node["kind"] == "folder" for node in nodes)


async def test_a_row_says_how_the_file_got_in_and_what_it_is_tagged_with(
    client, archive
) -> None:
    """The question this answers is the one search cannot: *what is in here?*"""
    response = await client.get(
        "/api/tree", params={"path": "U.S. Dept of Veterans Affairs/2009"}
    )
    nodes = response.json()["nodes"]
    assert len(nodes) == 1

    row = nodes[0]
    assert row["kind"] == "document"
    assert row["title"] == "Certificate of Release or Discharge"
    assert row["ingest_source"] == "web_upload"
    assert row["tags"] == ["military"]
    assert row["sensitivity"] == "vital"
    assert row["original_filename"] == "scan-dd214.pdf"
    assert row["document_date"] == "2009-06-14"


async def test_a_bundle_appears_once_carrying_its_page_range(client, archive) -> None:
    response = await client.get("/api/tree", params={"path": "_bundles"})
    folders = response.json()["nodes"]
    assert len(folders) == 1 and folders[0]["kind"] == "folder"

    inside = (await client.get("/api/tree", params={"path": folders[0]["path"]})).json()
    rows = inside["nodes"]
    # One file on disk, listed once — not three rows for one PDF.
    assert len(rows) == 1
    assert rows[0]["kind"] == "bundle"
    assert rows[0]["page_count"] == 12
    assert rows[0]["ingest_source"] == "watched_folder"


async def test_a_backup_produces_a_restorable_generation(session, archive) -> None:
    """The full path: dump, blobs, manifest — in the order the drill expects."""
    if not __import__("shutil").which("pg_dump"):
        pytest.skip("pg_dump is not installed in this image")

    report = await integrity.check(session, library_ids=[archive["library"].id])
    assert report.healthy

    destination = archive["tmp"] / "generation"
    result = backup.run_backup(report, destination=destination)

    assert result.database_dump.is_file() and result.database_dump.stat().st_size > 0
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["integrity"]["healthy"] is True

    # Every original the database references must be in the backup, which is
    # exactly the check the restore drill re-runs against the restored data.
    for sha in (archive["dd214_sha"], archive["bundle_sha"]):
        assert (destination / "blobs" / sha[:2] / sha[2:4] / sha).is_file()


def test_a_missing_pg_dump_is_reported_as_a_deployment_problem(monkeypatch, tmp_path) -> None:
    """A bare FileNotFoundError would read like the archive is broken. It isn't."""
    monkeypatch.setattr(backup.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="deployment problem"):
        backup.dump_database(tmp_path / "bindery.dump")


def test_connection_settings_survive_an_awkward_password(monkeypatch) -> None:
    """The real password on the deployed host broke a URL-on-the-command-line.

    pg_dump reported *invalid integer value ... for connection option "port"* —
    which reads like a misconfiguration and is actually the password being
    parsed as `host:port`. Passing libpq settings through the environment makes
    the question of what characters a password contains stop mattering.
    """
    from api.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://bindery:p%40ss%3Aw%2Frd@db.internal:6543/bindery",
    )
    try:
        env = backup.connection_env()
    finally:
        get_settings.cache_clear()

    assert env["PGHOST"] == "db.internal"
    assert env["PGPORT"] == "6543"
    assert env["PGDATABASE"] == "bindery"
    assert env["PGUSER"] == "bindery"
    # Percent-decoded, so what libpq receives is the actual password.
    assert env["PGPASSWORD"] == "p@ss:w/rd"
