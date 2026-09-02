"""Phase 6 — trust, export and resilience (T-6.1 to T-6.8).

The verification steps from the phase plan, in the plan's own order:

1. Corrupt a blob deliberately → the integrity check finds it (REQ-095)
2. The full export is navigable **without Bindery** (REQ-093)
3. Delete the mirror tree → it rebuilds; `_bundles/` lists page ranges (REQ-094, REQ-043)
4. Offsite copies are verified encrypted (REQ-096)
5. Restore drill (REQ-097) — the script itself, executed, against a real backup
6. Vital documents surface without searching (REQ-091)
7. The go-bag is produced and decryptable (REQ-092)
8. The audit log filters by document, actor and time (REQ-069)
"""

import asyncio
import hashlib
import json
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

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


async def test_the_integrity_check_does_not_hash_on_the_event_loop(
    session, archive, monkeypatch
) -> None:
    """CR-010: one uvicorn process serves every request from one event loop, so
    re-hashing the archive inside the handler is search, login, `/api/live` and
    the container healthcheck all stopped for the length of the scan — on the
    button whose whole purpose is reassurance."""
    import threading

    from api.export import integrity as module

    loop_thread = threading.get_ident()
    seen: list[int] = []
    hash_file = module.hash_file

    def spy(path):
        seen.append(threading.get_ident())
        return hash_file(path)

    monkeypatch.setattr(module, "hash_file", spy)
    report = await integrity.check(session, library_ids=[archive["library"].id])
    assert report.checked, "nothing was checked, so the test proves nothing"
    assert seen, "nothing was hashed"
    assert all(ident != loop_thread for ident in seen)


async def _worst_stall(run):
    """Run `run()` with a 5 ms heartbeat beside it and report the longest gap.

    The same measurement as `test_a_run_leaves_the_event_loop_free` in
    tests/test_offsite_replicate.py, and for the same reason: what matters is
    that the loop keeps turning, which survives a refactor that a grep for
    `to_thread` would not. The *longest* gap rather than an average, because an
    operation that threads four of its five blocking calls still freezes the
    process for the fifth and a mean would hide it.
    """
    stalls: list[float] = []

    async def heartbeat():
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.005)
            now = time.monotonic()
            stalls.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    try:
        result = await run()
    finally:
        beat.cancel()

    assert len(stalls) > 10, "the heartbeat never got to run at all"
    return result, max(stalls)


# --------------------------------------------------------------------------
# 2. Full export works without Bindery (T-6.3, REQ-093)
# --------------------------------------------------------------------------


async def test_a_full_export_does_not_copy_originals_on_the_event_loop(
    session, archive, monkeypatch
) -> None:
    """CR-010: `POST /export/full` copied every original inside its handler.

    One uvicorn process serves the whole application from one event loop, so an
    export of a real archive — hundreds of gigabytes of `shutil.copy2` — was
    search, login, `/api/live` and the container healthcheck all stopped until
    it finished. The healthcheck gives up after ~2.5 minutes, and nothing
    distinguishes that from a genuine outage.
    """
    real_copy = archive_export.shutil.copy2

    def slow_copy(source, target, *args, **kwargs):
        time.sleep(0.05)
        return real_copy(source, target, *args, **kwargs)

    monkeypatch.setattr(archive_export.shutil, "copy2", slow_copy)

    result, worst = await _worst_stall(
        lambda: archive_export.full_export(
            session, [archive["library"].id], destination=archive["tmp"] / "slow-export"
        )
    )

    assert result.file_count == 2, "nothing was copied, so the test proves nothing"
    # Every copy sleeps 50ms, so anything left on the loop shows up as a gap at
    # least that long. Off the loop, the gaps are the 5ms the heartbeat asked for.
    assert worst < 0.04, (
        f"the event loop was blocked for {worst * 1000:.0f}ms during a full "
        "export — every other request was frozen for it"
    )


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


async def test_a_mirror_rebuild_does_not_link_on_the_event_loop(
    session, archive, monkeypatch
) -> None:
    """CR-010, the same defect on `POST /mirror/rebuild`.

    A rebuild hardlinks one entry per source file, writes an index per bundle,
    and then walks the whole tree to prune it — over a real archive that is
    hundreds of thousands of filesystem calls, and every one of them ran on the
    loop serving every other request.
    """
    real_link = mirror._link_or_copy

    def slow_link(source, target):
        time.sleep(0.05)
        return real_link(source, target)

    monkeypatch.setattr(mirror, "_link_or_copy", slow_link)

    result, worst = await _worst_stall(
        lambda: mirror.rebuild(
            session, [archive["library"].id], root=archive["tmp"] / "slow-mirror"
        )
    )

    assert result.linked + result.copied == 2, "nothing was mirrored"
    assert worst < 0.04, (
        f"the event loop was blocked for {worst * 1000:.0f}ms during a mirror "
        "rebuild — every other request was frozen for it"
    )


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


DRILL_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "restore-drill.sh"


@pytest.fixture
def drill(tmp_path):
    """Run `scripts/restore-drill.sh` for real, against a real backup directory.

    CR-041. The drill is the deliverable of Phase 6 and the only thing that had
    ever tested it was a grep for six substrings — every one of which survives a
    script whose control flow is broken.

    **What is real here**: bash, the script itself, its argument guards, the
    manifest read, the blob and vault presence loops, the hit count, the exit
    codes and the cleanup trap; and a backup directory on disk laid out the way
    `api/export/backup.py` lays one out, sharded blob paths and all.

    **What is not**: Postgres. The suite runs in a container with no Docker
    socket, so `docker` is `tests/fake_docker.py` and the restore is asserted to
    have been *asked for*, not performed. The drill that restores a real dump
    into a real empty database runs in CI, in the e2e job, against the seeded
    corpus — see the "Restore drill" steps in .github/workflows/build.yml.
    """
    fake = Path(__file__).resolve().parent / "fake_docker.py"
    binary = tmp_path / "bin" / "docker"
    binary.parent.mkdir(parents=True)
    binary.write_text(f'#!/bin/sh\nexec python3 "{fake}" "$@"\n')
    binary.chmod(0o755)

    backup = tmp_path / "20260902-031500"
    (backup / "blobs").mkdir(parents=True)
    (backup / "bindery.dump").write_bytes(b"PGDMP fake custom-format archive")
    (backup / "manifest.json").write_text(
        json.dumps(
            {
                "created_at": datetime.now(UTC).isoformat(),
                "blob_count": 1,
                "blob_bytes": 42,
                "integrity": {"healthy": True},
            }
        )
    )

    sha = hashlib.sha256(b"a restored original").hexdigest()
    blob = backup / "blobs" / sha[:2] / sha[2:4] / sha
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"a restored original")

    answers = tmp_path / "answers"
    answers.mkdir()
    (answers / "shas").write_text(f"{sha}\n")
    (answers / "vault").write_text("")
    (answers / "hits").write_text("Certificate of Release or Discharge | pp. 1-1\n")
    (answers / "forms").write_text("")
    log = tmp_path / "docker.log"

    def run(term: str = "DD-214", **overrides):
        environment = {
            **os.environ,
            "PATH": f"{binary.parent}:{os.environ['PATH']}",
            "FAKE_DOCKER_LOG": str(log),
            "FAKE_DOCKER_SHAS": str(answers / "shas"),
            "FAKE_DOCKER_VAULT": str(answers / "vault"),
            "FAKE_DOCKER_HITS": str(answers / "hits"),
            "FAKE_DOCKER_FORMS": str(answers / "forms"),
            **{key: str(value) for key, value in overrides.items()},
        }
        return subprocess.run(
            ["bash", str(DRILL_SCRIPT), str(backup), term],
            capture_output=True, text=True, env=environment, timeout=120,
        )

    return {
        "run": run, "backup": backup, "blob": blob, "sha": sha,
        "answers": answers, "log": log, "tmp": tmp_path,
    }


def test_the_restore_drill_passes_against_a_backup_that_is_whole(drill) -> None:
    """The deliverable, executed rather than described.

    `I restored to an empty container and found the DD-214` — the script really
    runs, really reads the backup directory, really counts the originals it
    references and really reports a pass.
    """
    result = drill["run"]()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "DRILL PASSED" in result.stdout
    assert "all 1 originals present" in result.stdout

    # It must never point at the live database. Asserted from what the script
    # actually did rather than from the text of the script: every container it
    # touched is the throwaway one, and the dump went nowhere else.
    calls = drill["log"].read_text().splitlines()
    containers = {line.split("\t")[1] for line in calls if line.startswith("exec\t")}
    assert containers == {"bindery-restore-drill"}
    assert any("pg_restore" in line and "--exit-on-error" in line for line in calls)
    # And it tore its scratch container down, on the way out, via the trap.
    assert calls[-1].startswith("rm\t-f\tbindery-restore-drill")


def test_the_restore_drill_fails_loudly_when_it_cannot_find_the_document(drill) -> None:
    """The drill's whole value is that it can fail.

    A drill that always passes is a ceremony. So the search comes back empty and
    the script has to notice: a restore that completed without error, over a
    backup with every blob present, is still a failed drill if the document
    cannot be retrieved afterwards.
    """
    (drill["answers"] / "hits").write_text("")
    (drill["answers"] / "forms").write_text("")

    result = drill["run"]()

    assert result.returncode == 1, result.stdout + result.stderr
    assert "DRILL FAILED" in result.stdout
    assert "DRILL PASSED" not in result.stdout


def test_a_hit_with_no_title_still_counts_as_finding_the_document(drill) -> None:
    """`NULL || text` is NULL in SQL, and an unclassified document has no title.

    Without the `coalesce` in the search query a real hit comes back as a blank
    line, `grep -c .` counts nothing, and the drill reports failure while
    holding the document it was asked for. That is not hypothetical — it is
    what this drill did on its first run against a live archive. The blank line
    is what the bug produced, so that is what is fed in here.
    """
    (drill["answers"] / "hits").write_text("\n")

    result = drill["run"]()

    assert result.returncode == 1, "a blank line is not a document"
    assert "DRILL FAILED" in result.stdout

    # And the same query, coalesced, is a pass.
    (drill["answers"] / "hits").write_text("untitled | pp. 4-8\n")
    assert drill["run"]().returncode == 0


def test_the_restore_drill_fails_when_an_original_is_not_in_the_backup(drill) -> None:
    """A backup that restores and cannot supply its originals is not a backup.

    `pg_restore` returning zero says nothing about the blob pool beside it, and
    this is the check that does. The loop that finds this is the one a substring
    test cannot tell apart from a loop over an empty list.
    """
    drill["blob"].unlink()

    result = drill["run"]()

    assert result.returncode == 1, result.stdout + result.stderr
    assert "MISSING BLOB" in result.stdout
    assert "not restorable" in result.stdout
    assert "DRILL PASSED" not in result.stdout


def test_the_restore_drill_fails_when_a_sealed_vault_object_is_not_in_the_backup(
    drill,
) -> None:
    """The vault is the one part of the archive with no second source.

    A blob can be re-scanned. A sealed object exists once, so a backup missing
    one is unrecoverable and the drill has to say so rather than pass.
    """
    name = "beefcafe" + "0" * 56
    (drill["answers"] / "vault").write_text(f"{name}\t{'a' * 64}\n")

    result = drill["run"]()

    assert result.returncode == 1, result.stdout + result.stderr
    assert "MISSING VAULT OBJECT" in result.stdout
    assert "the vault is not restorable" in result.stdout


def test_the_restore_drill_stops_when_the_restore_itself_fails(drill) -> None:
    """`set -e`, exercised. A failed `pg_restore` must not reach the search."""
    result = drill["run"](FAKE_DOCKER_RESTORE_EXIT=1)

    assert result.returncode != 0
    assert "restore completed without error" not in result.stdout
    assert "DRILL PASSED" not in result.stdout


def test_the_restore_drill_refuses_a_backup_that_is_not_there(drill) -> None:
    """The guards run before anything is started, and they exit non-zero."""
    missing = drill["tmp"] / "no-such-generation"
    environment = {**os.environ, "PATH": f"{drill['tmp']}/bin:{os.environ['PATH']}"}

    absent = subprocess.run(
        ["bash", str(DRILL_SCRIPT), str(missing)],
        capture_output=True, text=True, env=environment, timeout=60,
    )
    assert absent.returncode == 1
    assert "no such backup directory" in absent.stdout

    empty = drill["tmp"] / "empty-generation"
    empty.mkdir()
    no_dump = subprocess.run(
        ["bash", str(DRILL_SCRIPT), str(empty)],
        capture_output=True, text=True, env=environment, timeout=60,
    )
    assert no_dump.returncode == 1
    assert "no bindery.dump" in no_dump.stdout

    # No argument at all: the usage guard, and `set -u` behind it.
    unspecified = subprocess.run(
        ["bash", str(DRILL_SCRIPT)],
        capture_output=True, text=True, env=environment, timeout=60,
    )
    assert unspecified.returncode != 0
    assert "usage" in unspecified.stderr


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


async def test_a_go_bag_does_not_encrypt_on_the_event_loop(
    session, archive, monkeypatch
) -> None:
    """CR-010, the same defect on `POST /export/go-bag`.

    Deflating and AES-encrypting the vital tier is seconds of solid CPU inside
    the request handler, and it held the whole process while it ran.
    """
    pytest.importorskip("pyzipper")

    real_index = archive_export.index_html

    def slow_index(*args, **kwargs):
        # 100ms rather than 50: the zip is written by a single call, so this is
        # the only place to put the delay and the run has to last long enough
        # for the heartbeat to be sampled a useful number of times.
        time.sleep(0.1)
        return real_index(*args, **kwargs)

    monkeypatch.setattr(archive_export, "index_html", slow_index)

    result, worst = await _worst_stall(
        lambda: archive_export.go_bag(
            session,
            [archive["library"].id],
            "correct-horse-battery-staple",
            destination=archive["tmp"] / "slow-go-bag.zip",
        )
    )

    assert result.file_count == 1, "nothing was written into the go-bag"
    assert worst < 0.04, (
        f"the event loop was blocked for {worst * 1000:.0f}ms while a go-bag "
        "was written — every other request was frozen for it"
    )


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
    """The real deployed password broke two parsers before this one.

    It is base64-ish and contains `/`. Handed to pg_dump as a URL that produced
    *invalid integer value ... for connection option "port"*; parsed with
    `urlsplit` it produced *Port could not be cast to integer*, because
    `urlsplit` reads the first `/` as the start of the path. Both read like
    misconfiguration and both are parsing bugs.

    `make_url` is used because it is the parser that already makes the
    application's own connection work — whatever it accepts as a password, the
    backup must accept too, or backups fail on exactly the hosts that are
    running fine.
    """
    from api.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv(
        "DATABASE_URL",
        # Unescaped on purpose: this is the shape that actually shipped.
        "postgresql+asyncpg://bindery:aB3/xY9/qq/Zz@db.internal:6543/bindery",
    )
    try:
        env = backup.connection_env()
    finally:
        get_settings.cache_clear()

    assert env["PGHOST"] == "db.internal"
    assert env["PGPORT"] == "6543"
    assert env["PGDATABASE"] == "bindery"
    assert env["PGUSER"] == "bindery"
    assert env["PGPASSWORD"] == "aB3/xY9/qq/Zz"

    # The failure this replaces: urlsplit reads the first `/` as the path.
    from urllib.parse import urlsplit

    with pytest.raises(ValueError):
        _ = urlsplit(
            "postgresql://bindery:aB3/xY9/qq/Zz@db.internal:6543/bindery"
        ).port


async def test_the_dump_is_readable_by_the_restore_tool(session, archive) -> None:
    """The gap the restore drill found on the real host.

    `pg_dump` 17 dumps a Postgres 16 server perfectly well and writes an archive
    `pg_restore` 16 refuses: *unsupported version (1.16) in file header*. A
    backup that cannot be restored is not a backup, and the failure only appears
    at restore time — which is exactly the moment it must not.

    So the dump is checked here for being *readable*, not merely non-empty.
    """
    import shutil
    import subprocess

    if not shutil.which("pg_dump"):
        pytest.skip("pg_dump is not installed in this image")

    report = await integrity.check(session, library_ids=[archive["library"].id])
    result = backup.run_backup(report, destination=archive["tmp"] / "readable")

    listing = subprocess.run(
        ["pg_restore", "--list", str(result.database_dump)],
        capture_output=True, check=False,
    )
    assert listing.returncode == 0, (
        "pg_restore cannot read the archive pg_dump just wrote — the client and "
        f"server versions have drifted apart:\n{listing.stderr.decode()}"
    )
    assert b"document" in listing.stdout, "the dump should contain the schema"

# --------------------------------------------------------------------------
# Who may ask for a replication run (CR-006, ADR-009, ADR-010)
# --------------------------------------------------------------------------


@pytest.fixture
async def offsite_configured(session):
    """A complete destination, and no history — then put both back.

    Settings are account-wide and the suite migrates once per session, so a
    module that leaves a bucket configured changes what every later module
    sees.
    """
    from api import settings_store
    from api.db.models import OffsiteRun, Setting

    await session.execute(sa.delete(OffsiteRun))
    for key, value in (
        (settings_store.AWS_ACCESS_KEY_ID, "AKIAIOSFODNN7EXAMPLE"),
        (settings_store.AWS_SECRET_ACCESS_KEY, "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
        (settings_store.OFFSITE_BUCKET, "bindery-offsite-authorisation"),
        (settings_store.OFFSITE_REGION, "us-east-1"),
        (settings_store.OFFSITE_KMS_KEY_ID, "alias/bindery-offsite"),
    ):
        await settings_store.set_(session, key, value, actor_id=None)
    await session.commit()

    yield

    await session.execute(sa.delete(OffsiteRun))
    await session.execute(
        sa.delete(Setting).where(
            Setting.key.in_(
                [
                    settings_store.AWS_ACCESS_KEY_ID,
                    settings_store.AWS_SECRET_ACCESS_KEY,
                    settings_store.OFFSITE_BUCKET,
                    settings_store.OFFSITE_REGION,
                    settings_store.OFFSITE_KMS_KEY_ID,
                ]
            )
        )
    )
    await session.commit()


async def test_a_household_member_cannot_ask_for_a_replication_run(
    client, session, signed_in, offsite_configured
) -> None:
    """A run is a whole-host operation, and `_visible` is not a claim about the host.

    It pg_dumps every library and ships every blob and every vault object to
    the configured bucket. Belonging to a library says nothing about whether
    you may start that.
    """
    from api.db.models import OffsiteRun

    await signed_in()

    response = await client.post("/api/offsite/replicate")
    assert response.status_code == 403
    assert "administrator" in response.text

    runs = (await session.execute(sa.select(OffsiteRun))).scalars().all()
    assert runs == [], "a refused request must not queue a run"


async def test_reading_the_replication_status_stays_open_to_every_member(
    client, signed_in
) -> None:
    """"Has a copy left the building?" is a question about your own documents.

    The gate is on the trigger, not on the answer — hiding the status from
    household members would make the archive less trustworthy, not more.
    """
    await signed_in()
    assert (await client.get("/api/offsite")).status_code == 200


async def test_an_administrator_can_still_ask_for_a_run(
    client, session, signed_in, offsite_configured
) -> None:
    from api import offsite_runs
    from api.db.models import OffsiteRun

    user, _ = await signed_in()
    user.is_admin = True
    await session.commit()

    assert (await client.post("/api/offsite/replicate")).status_code == 200
    run = (await session.execute(sa.select(OffsiteRun))).scalars().one()
    assert run.state == offsite_runs.REQUESTED
