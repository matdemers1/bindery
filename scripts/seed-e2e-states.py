"""Give the e2e suite the two job states it exists to check.

`scripts/seed-demo.py` produces a healthy archive, which is the right fixture
for screenshots and the wrong one for the pipeline specs: with nothing declined
and nothing dead-lettered they skip, and two tests that always skip are two
tests that have stopped saying anything.

So this adds exactly one of each, on invented files:

- a **declined** job, standing in for the 10x5 pixel image the real archive
  refused — it must be listed and must offer nothing to acknowledge;
- a **dead-lettered** job, which must light the badge and must survive being
  acknowledged, with its error intact;
- a document **waiting for review**, so the correction specs (Phase 17) have
  something to correct. Without it the queue is empty on a healthy seeded
  archive and the correct-then-accept test skips — which is the same nothing
  the two above were added to stop.
- a **vault with a document and a photograph in it** (CR-127). `vault.spec.ts`
  created a vault and deliberately put nothing in it, so `/api/vault/items` was
  always empty, the documents/photos split and the decrypted-image render never
  ran, and the move-in path — the only code in Bindery permitted to delete a
  person's original (ADR-012) — had no browser coverage at all. Two tests that
  always skip are two tests that have stopped saying anything, which is the
  sentence at the top of this file.

Piped into the api container rather than run from it: the runtime image ships
`api/` and `alembic/` and deliberately not `scripts/`.

    docker compose exec -T api python - < scripts/seed-e2e-states.py
"""

import asyncio
import os

import sqlalchemy as sa

from api.db.enums import IngestSource, JobStage, JobState, ReviewState, SourceFileState
from api.db.models import (
    AppUser,
    Document,
    FieldSource,
    Job,
    Library,
    Membership,
    Page,
    SourceFile,
)
from api.db.session import SessionFactory

EMAIL = os.environ.get("BINDERY_EMAIL", "demo@example.com")

CASES = [
    (
        "laser-cutting-asset.png",
        JobState.DECLINED,
        "PermanentFailure('10x5 pixels is too small to be a document page "
        "(each side must be at least 16px). This is usually a design or "
        "laser-cutting asset rather than a document.')",
    ),
    (
        "scan-that-timed-out.pdf",
        JobState.DEAD_LETTER,
        "TimeoutError('OCR did not finish within 600s')",
    ),
]


async def main() -> None:
    async with SessionFactory() as session:
        user = (
            await session.execute(sa.select(AppUser).where(AppUser.email == EMAIL))
        ).scalar_one_or_none()
        if user is None:
            raise SystemExit(f"no such user: {EMAIL}")

        library_id = (
            await session.execute(
                sa.select(Membership.library_id).where(Membership.user_id == user.id).limit(1)
            )
        ).scalar_one_or_none()
        if library_id is None:
            raise SystemExit(f"{EMAIL} owns no library")
        name = (
            await session.execute(sa.select(Library.name).where(Library.id == library_id))
        ).scalar_one()

        for filename, state, error in CASES:
            existing = (
                await session.execute(
                    sa.select(SourceFile).where(
                        SourceFile.library_id == library_id,
                        SourceFile.original_filename == filename,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                # Establish the state, do not merely notice the row. The suite
                # acknowledges this dead letter, so a second run found it
                # already acknowledged and failed on a fixture rather than on
                # the behaviour. A seed that is not idempotent works exactly
                # once, which is the same as not working.
                job = (
                    await session.execute(
                        sa.select(Job).where(Job.source_file_id == existing.id)
                    )
                ).scalar_one_or_none()
                if job is not None:
                    job.state = state
                    job.acknowledged_at = None
                    job.last_error = error
                print(f"{filename} reset to {state.value}")
                continue

            blob = await _real_blob(_tiny_pdf(filename))
            source = SourceFile(
                library_id=library_id,
                sha256=blob.sha256,
                byte_size=blob.byte_size,
                original_filename=filename,
                ingest_source=IngestSource.WEB_UPLOAD,
            )
            session.add(source)
            await session.flush()
            session.add(
                Job(
                    source_file_id=source.id,
                    stage=JobStage.NORMALIZE,
                    state=state,
                    # A refusal is reached on the first look; a dead letter has
                    # spent its whole budget. The screen says both.
                    attempts=1 if state is JobState.DECLINED else 5,
                    last_error=error,
                )
            )
            print(f"{filename} -> {state.value} in {name}")

        # Everything already dead-lettered gets acknowledged, so the one this
        # script creates is the *only* thing outstanding. Without it the badge
        # assertion depends on whatever else the archive happens to be carrying
        # — locally that was 27 classify failures reachable through
        # `document_id` rather than `source_file_id`, which is exactly the kind
        # of thing a fixture exists to remove from the question.
        from datetime import UTC, datetime

        from api.db import repository

        others = (
            await session.execute(
                sa.select(Job).where(
                    Job.state == JobState.DEAD_LETTER,
                    Job.acknowledged_at.is_(None),
                    Job.id.in_(repository.visible_job_ids([library_id])),
                )
            )
        ).scalars().all()
        now = datetime.now(UTC)
        settled = 0
        for job in others:
            source = await session.get(SourceFile, job.source_file_id) if job.source_file_id else None
            if source is not None and source.original_filename == "scan-that-timed-out.pdf":
                continue  # the one the suite is here to acknowledge itself
            job.acknowledged_at = now
            settled += 1
        if settled:
            print(f"acknowledged {settled} pre-existing dead letter(s) to make the state known")

        await _document_awaiting_review(session, library_id)
        await session.flush()
        await _a_vault_with_something_in_it(session, user, library_id)

        await session.commit()


async def _document_awaiting_review(session, library_id) -> None:
    """One document the gate declined to file, for the correction specs.

    Given a title the model would plausibly produce and a person would
    plausibly want to fix, because that is the case Phase 17 exists for.
    """
    filename = "needs-a-correction.pdf"
    existing = (
        await session.execute(
            sa.select(SourceFile).where(
                SourceFile.library_id == library_id,
                SourceFile.original_filename == filename,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        blob = await _real_blob(_tiny_pdf(filename))
        existing = SourceFile(
            library_id=library_id,
            sha256=blob.sha256,
            byte_size=blob.byte_size,
            original_filename=filename,
            ingest_source=IngestSource.WEB_UPLOAD,
            page_count=1,
            state=SourceFileState.PROCESSED,
        )
        session.add(existing)
        await session.flush()
        session.add(
            Page(
                source_file_id=existing.id,
                page_number=1,
                text="Harbour Utilities quarterly statement, account 4417.",
            )
        )

    document = (
        await session.execute(
            sa.select(Document).where(Document.source_file_id == existing.id)
        )
    ).scalar_one_or_none()
    if document is None:
        document = Document(
            library_id=library_id,
            source_file_id=existing.id,
            page_start=1,
            page_end=1,
        )
        session.add(document)

    # Reset every run, not just on creation. The correction specs edit this
    # document and the screenshot capture photographs it, so a fixture that
    # kept whatever the last test typed would drift into the documentation —
    # which is how "Corrected by the e2e run" ended up in review.png once.
    document.title = "Harbour Utilties - Statment"  # the misspelling is the point
    document.review_state = ReviewState.NEEDS_REVIEW
    await session.flush()

    # And give the fields back, or the second run starts with them claimed by
    # whoever the first run signed in as.
    await session.execute(
        sa.delete(FieldSource).where(FieldSource.document_id == document.id)
    )
    print(f"{filename} is waiting for review")


# ---------------------------------------------------------------------------
# The vault (CR-127)
# ---------------------------------------------------------------------------

# The same two secrets `web/e2e/vault.spec.ts` uses. They live in a public test
# fixture on purpose: this vault holds two files this script invented thirty
# lines ago, in a database CI throws away.
VAULT_PASSPHRASE = "an end to end vault passphrase"
VAULT_PIN = "481516"

# A 1x1 PNG. Small enough to inline, real enough that `is_image` is true and the
# photos tab has something to render through `/api/vault/items/{id}/original`.
ONE_PIXEL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c636064f8cf000001830103b0b7a3b6"
    "0000000049454e44ae426082"
)

VAULTED = [
    ("a-sealed-record.pdf", "application/pdf", "pdf", "Sealed — discharge papers"),
    ("a-sealed-photograph.png", "image/png", "png", "Sealed — a photograph"),
]


async def _real_blob(payload: bytes):
    """Store bytes and hand back the blob record.

    The two fixtures below used to invent a digest with `uuid4().hex * 2`,
    which reads as harmless — the rows only exist so the pipeline screen has a
    refusal and a dead letter to show. But `integrity.check` walks every
    SourceFile looking for its original, so those three rows made the archive
    permanently "missing 3", `run_backup` refused over a failing check, and the
    restore drill CI step could never once have passed. A fixture that lies
    about a content address is a fixture that breaks the thing that verifies
    content addresses.
    """
    from api.storage.blobs import store_stream

    async def one_chunk(data: bytes):
        yield data

    return await store_stream(one_chunk(payload))


def _stamped_png(stamp: str) -> bytes:
    """The pixel, with `stamp` written into a tEXt chunk before IEND.

    Content addresses are the whole storage model, and `store.seal` refuses to
    vault a blob two source files share — correctly, since unlinking it would
    destroy somebody else's original. A byte-identical fixture seeded into two
    libraries is exactly that case, so the stamp makes each one its own file.
    """
    import struct
    import zlib

    data = b"Comment\x00" + stamp.encode()
    chunk = struct.pack(">I", len(data)) + b"tEXt" + data
    chunk += struct.pack(">I", zlib.crc32(b"tEXt" + data) & 0xFFFFFFFF)
    iend = ONE_PIXEL_PNG.rindex(b"\x00\x00\x00\x00IEND")
    return ONE_PIXEL_PNG[:iend] + chunk + ONE_PIXEL_PNG[iend:]


def _tiny_pdf(stamp: str = "") -> bytes:
    """One page, valid enough to be stored and served back after decryption."""
    content = f"BT /F1 12 Tf 60 720 Td (A sealed record. {stamp}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode()
        + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    import io

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


async def _a_vault_with_something_in_it(session, user, library_id) -> None:
    """Set up the demo account's vault and seal one document and one photograph.

    Sealing here rather than through the API because the browser cannot: the
    move-in path needs the unlock session, and the unlock session lives in the
    api *process*. This runs inside that container but in a different process,
    so it opens the vault with the passphrase and hands the data key to
    `store.seal` directly — the same function the route calls.

    This is the one script that reaches the only sanctioned delete path in the
    project (ADR-012), and it does so on two files it created moments earlier,
    in a database CI throws away. It is not a general-purpose tool.
    """
    from api.db.models import Vault, VaultItem
    from api.storage.blobs import store_stream
    from api.vault import service as vault_service
    from api.vault import store as vault_store

    vault = (
        await session.execute(sa.select(Vault).where(Vault.user_id == user.id))
    ).scalar_one_or_none()
    if vault is None:
        vault = await vault_service.create(session, user.id, VAULT_PASSPHRASE, VAULT_PIN)
        print("vault created for the demo account")
        data_key = vault_service.sessions.key(user.id)
    else:
        # A vault that already exists may have a different passphrase (an
        # earlier spec run created one). If it does, say so loudly rather than
        # leaving the suite to fail on an unlock it cannot explain.
        try:
            data_key = await vault_service.unlock_with_passphrase(
                session, vault, VAULT_PASSPHRASE
            )
        except Exception as error:  # noqa: BLE001 — reported, not handled
            raise SystemExit(
                "the demo account already has a vault this script cannot open "
                f"({error}). Drop the database or the `vault` row and re-seed; "
                "the e2e suite needs a vault whose PIN it knows."
            ) from error

    existing = (
        await session.execute(
            sa.select(sa.func.count()).select_from(VaultItem).where(
                VaultItem.vault_id == vault.id
            )
        )
    ).scalar_one()
    if existing >= len(VAULTED):
        print(f"vault already holds {existing} item(s)")
        return

    async def one_chunk(data: bytes):
        yield data

    stamp = str(library_id)
    for filename, mime_type, kind, title in VAULTED:
        already = (
            await session.execute(
                sa.select(SourceFile).where(
                    SourceFile.library_id == library_id,
                    SourceFile.original_filename == filename,
                )
            )
        ).scalar_one_or_none()
        if already is not None:
            print(f"{filename} is already in the archive; leaving it alone")
            continue

        payload = _stamped_png(stamp) if kind == "png" else _tiny_pdf(stamp)
        blob = await store_stream(one_chunk(payload))
        source = SourceFile(
            library_id=library_id,
            sha256=blob.sha256,
            byte_size=blob.byte_size,
            original_filename=filename,
            mime_type=mime_type,
            ingest_source=IngestSource.WEB_UPLOAD,
            page_count=1,
            state=SourceFileState.PROCESSED,
        )
        session.add(source)
        await session.flush()
        session.add(
            Page(
                source_file_id=source.id,
                page_number=1,
                text="Sealed page text. Nothing outside the vault may quote this.",
            )
        )
        document = Document(
            library_id=library_id,
            source_file_id=source.id,
            page_start=1,
            page_end=1,
            title=title,
            review_state=ReviewState.FILED,
        )
        session.add(document)
        await session.flush()

        await vault_store.seal(session, document, source, vault.id, user.id, data_key)
        print(f"{filename} sealed into the vault as {title!r}")

    # Shut it again. Every vault spec establishes the state it needs, and the
    # one that asserts a locked vault says nothing must not start with an open
    # one left over from the seed.
    vault_service.sessions.lock(user.id)


if __name__ == "__main__":
    asyncio.run(main())
