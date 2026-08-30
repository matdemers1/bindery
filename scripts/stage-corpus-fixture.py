#!/usr/bin/env python3
"""Stage a real document as a golden-corpus fixture (T-9.5, REQ-018).

    docker compose --env-file .env -f infra/docker-compose.yml exec api \
        python scripts/stage-corpus-fixture.py <source-file-id> dd214

Writes `tests/corpus/<name>/source.pdf` and an `expected.txt` **pre-filled with
what OCR currently reads**, so the job becomes correcting errors rather than
typing a document out. Then:

    make ocr-report

The distinction this preserves is the whole point of the corpus. `expected.txt`
must be what the page *says*, verified by a person who looked at it. If it were
generated from OCR and left uncorrected, the report would score OCR against
itself and return 100% while telling you nothing — a number that looks like
success and measures the absence of a measurement.

So the file is written with every line marked, and `make ocr-report` refuses a
fixture that still carries the marker. Correcting a page takes a few minutes;
transcribing one from nothing takes an hour, which is why this has been
outstanding since Phase 1.
"""

import asyncio
import shutil
import sys
from pathlib import Path

import sqlalchemy as sa

from api.db.models import Page, SourceFile
from api.db.session import SessionFactory
from api.storage.blobs import blob_path

REPO = Path(__file__).resolve().parent.parent
UNVERIFIED = "# UNVERIFIED — delete this line once you have checked the text below"


async def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    source_file_id, name = sys.argv[1], sys.argv[2]

    async with SessionFactory() as session:
        source_file = await session.get(SourceFile, source_file_id)
        if source_file is None:
            print(f"no source file {source_file_id}", file=sys.stderr)
            return 1
        pages = (
            await session.execute(
                sa.select(Page.page_number, Page.text)
                .where(Page.source_file_id == source_file.id)
                .order_by(Page.page_number)
            )
        ).all()

    target = REPO / "tests" / "corpus" / name
    target.mkdir(parents=True, exist_ok=True)

    suffix = Path(source_file.original_filename or "source.pdf").suffix or ".pdf"
    shutil.copyfile(blob_path(source_file.sha256), target / f"source{suffix}")

    body = "\n\n".join((text or "").strip() for _, text in pages)
    (target / "expected.txt").write_text(f"{UNVERIFIED}\n{body}\n")

    print(f"staged tests/corpus/{name}/")
    print(f"  source{suffix}   {source_file.original_filename}")
    print(f"  expected.txt   {len(pages)} page(s), {len(body)} characters of OCR output")
    print()
    print("Now read the pages and correct expected.txt, then delete its first")
    print("line. `make ocr-report` refuses a fixture that still carries it —")
    print("scoring OCR against its own output returns 100% and measures nothing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
