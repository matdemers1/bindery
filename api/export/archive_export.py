"""Exports that outlive Bindery (T-6.2, T-6.3, REQ-092, REQ-093).

> The full export works **without Bindery**.

That promise is what makes trusting an unproven personal project with
irreplaceable records a rational decision rather than an optimistic one. If this
software is abandoned, or the container won't start, or you simply want your
papers back, an export is a semantic folder tree of untouched originals plus a
static HTML index you open in a browser. No database, no server, no Python.

Three shapes:

- **Full export** — every original, laid out by correspondent and year, with a
  browsable index and the same metadata as JSON.
- **Go-bag** — the vital tier only, encrypted, small enough to carry (REQ-092).
- **Packet** — one correspondent's documents, for handing to an accountant.

Originals are copied byte-for-byte. Nothing is re-encoded, because a re-encoded
deed is a worse artifact than the one you scanned.
"""

import html
import json
import logging
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.db.enums import Sensitivity
from api.db.models import (
    Correspondent,
    Document,
    DocumentTag,
    DocumentType,
    KnownForm,
    SourceFile,
    Tag,
    live_tag_links,
)
from api.export.tree import (
    bundle_folder,
    deduplicate,
    document_filename,
    document_folder,
    safe_component,
)
from api.segments import live
from api.storage.blobs import blob_path

log = logging.getLogger("bindery.export")

BUNDLES_DIR = "_bundles"


def export_root() -> Path:
    return get_settings().data_root / "exports"


@dataclass
class ExportResult:
    path: Path
    document_count: int
    file_count: int
    byte_size: int
    encrypted: bool = False
    missing_blobs: list[str] = field(default_factory=list)


@dataclass
class _Entry:
    """One document, denormalised for laying out and for the JSON sidecar."""

    document: dict
    sha256: str
    original_filename: str | None
    page_count: int
    received_at: datetime
    relative_path: str | None = None


async def collect(
    session: AsyncSession,
    library_ids: list[uuid.UUID],
    *,
    vital_only: bool = False,
    correspondent_id: uuid.UUID | None = None,
) -> list[_Entry]:
    conditions: list[sa.ColumnElement[bool]] = [
        Document.library_id.in_(library_ids),
        live(),
    ]
    if vital_only:
        conditions.append(Document.sensitivity == Sensitivity.VITAL)
    if correspondent_id is not None:
        conditions.append(Document.correspondent_id == correspondent_id)

    rows = (
        await session.execute(
            sa.select(
                Document,
                SourceFile.sha256,
                SourceFile.original_filename,
                SourceFile.page_count,
                SourceFile.received_at,
                SourceFile.ingest_source,
                SourceFile.byte_size,
                Correspondent.name.label("correspondent"),
                DocumentType.name.label("document_type"),
                KnownForm.code.label("known_form"),
                KnownForm.name.label("known_form_name"),
            )
            .join(SourceFile, SourceFile.id == Document.source_file_id)
            .outerjoin(Correspondent, Correspondent.id == Document.correspondent_id)
            .outerjoin(DocumentType, DocumentType.id == Document.document_type_id)
            .outerjoin(KnownForm, KnownForm.id == Document.known_form_id)
            .where(sa.and_(*conditions))
            .order_by(SourceFile.received_at, Document.page_start)
        )
    ).all()

    entries: list[_Entry] = []
    for row in rows:
        document = row[0]
        tags = (
            (
                await session.execute(
                    sa.select(Tag.name)
                    .join(DocumentTag, DocumentTag.tag_id == Tag.id)
                    .where(DocumentTag.document_id == document.id, live_tag_links())
                    .order_by(Tag.name)
                )
            )
            .scalars()
            .all()
        )
        entries.append(
            _Entry(
                document={
                    "document_id": str(document.id),
                    "source_file_id": str(document.source_file_id),
                    "title": document.title,
                    "summary": document.summary,
                    "document_date": (
                        document.document_date.isoformat()
                        if document.document_date
                        else None
                    ),
                    "correspondent": row.correspondent,
                    "document_type": row.document_type,
                    "known_form": row.known_form,
                    "known_form_name": row.known_form_name,
                    "tags": list(tags),
                    "sensitivity": str(document.sensitivity),
                    "page_start": document.page_start,
                    "page_end": document.page_end,
                    "sha256": row.sha256,
                    "original_filename": row.original_filename,
                    "received_at": row.received_at.isoformat(),
                    # How it got in, and what state it is in — the two things
                    # you want when browsing rather than searching.
                    "ingest_source": str(row.ingest_source),
                    "review_state": str(document.review_state),
                    "byte_size": row.byte_size,
                },
                sha256=row.sha256,
                original_filename=row.original_filename,
                page_count=row.page_count or 1,
                received_at=row.received_at,
            )
        )
    return entries


def plan_layout(entries: list[_Entry]) -> dict[str, list[_Entry]]:
    """Decide where every original lands, and stamp it back onto each entry.

    Grouped by source file, because the unit of copying is the *file* — a bundle
    holding twelve documents is still one PDF on disk, and it is copied once.
    """
    by_file: dict[str, list[_Entry]] = {}
    for entry in entries:
        by_file.setdefault(entry.sha256, []).append(entry)

    taken: dict[tuple[str, ...], set[str]] = {}

    def claim(folder: tuple[str, ...], name: str) -> str:
        return deduplicate(name, taken.setdefault(folder, set()))

    for sha, group in by_file.items():
        extension = Path(group[0].original_filename or "").suffix or ".pdf"

        if len(group) == 1:
            # A whole-file document: it becomes a real file with a real name.
            entry = group[0]
            date = entry.document["document_date"]
            folder = document_folder(
                entry.document["correspondent"],
                int(date[:4]) if date else entry.received_at.year,
            )
            name = claim(
                folder,
                document_filename(
                    title=entry.document["title"] or entry.original_filename,
                    document_date=date,
                    extension=extension,
                ),
            )
            entry.relative_path = "/".join((*folder, name))
            continue

        # A bundle. The original cannot be split — originals are never modified
        # — so it gets a directory with an index describing its page ranges.
        folder_name = claim(
            (BUNDLES_DIR,),
            bundle_folder(
                original_filename=group[0].original_filename,
                received=group[0].received_at.date().isoformat(),
                pages=group[0].page_count,
            ),
        )
        original = f"{BUNDLES_DIR}/{folder_name}/original{extension}"
        for entry in group:
            entry.relative_path = original
            entry.document["bundle"] = f"{BUNDLES_DIR}/{folder_name}"
        log.debug("bundle %s → %s (%s documents)", sha[:8], folder_name, len(group))

    return by_file


def _page_range(entry: _Entry) -> str:
    start, end = entry.document["page_start"], entry.document["page_end"]
    return f"p. {start}" if start == end else f"pp. {start}–{end}"


_STYLE = """
 body{font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;margin:2rem auto;
      max-width:70rem;padding:0 1rem;color:#111;background:#fff}
 h1{font-size:1.45rem;margin-bottom:.2rem} h2{font-size:1.05rem;margin-top:2rem}
 p.note{color:#555;margin-top:.2rem}
 table{border-collapse:collapse;width:100%;margin-top:1.25rem}
 th,td{text-align:left;padding:.45rem .6rem;border-bottom:1px solid #ddd;vertical-align:top}
 th{border-bottom:2px solid #999;font-size:.75rem;text-transform:uppercase;letter-spacing:.04em}
 .mono{font-family:ui-monospace,Menlo,monospace;font-size:.85rem;color:#555;white-space:nowrap}
 a{color:#0645ad}
 @media (prefers-color-scheme:dark){
   body{background:#111;color:#eee} a{color:#8ab4f8}
   th,td{border-color:#333} th{border-bottom-color:#666} p.note,.mono{color:#aaa}
 }
"""


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>\n"
    )


def index_html(entries: list[_Entry], title: str, *, prefix: str = "") -> str:
    """A browsable index that needs nothing but a browser.

    Deliberately one self-contained file, inline styles, no scripts: it has to
    still work in fifteen years, opened from a USB stick, by someone who has
    never heard of this project.
    """
    rows = []
    for entry in sorted(
        entries,
        key=lambda e: (
            e.document["document_date"] or "9999",
            e.document["title"] or "",
        ),
    ):
        document = entry.document
        href = html.escape(prefix + (entry.relative_path or ""))
        label = document["title"] or entry.original_filename or document["sha256"][:12]
        kind = (
            document["document_type"]
            or document["known_form_name"]
            or document["known_form"]
            or ""
        )
        rows.append(
            "<tr>"
            f"<td class='mono'>{html.escape(document['document_date'] or '—')}</td>"
            f"<td><a href='{href}'>{html.escape(label)}</a></td>"
            f"<td>{html.escape(document['correspondent'] or '')}</td>"
            f"<td>{html.escape(kind)}</td>"
            f"<td>{html.escape(', '.join(document['tags']))}</td>"
            f"<td class='mono'>{_page_range(entry)}</td>"
            "</tr>"
        )

    body = (
        f"<h1>{html.escape(title)}</h1>"
        f"<p class='note'>{len(entries)} documents · exported "
        f"{datetime.now(UTC).strftime('%Y-%m-%d')}</p>"
        "<p class='note'>Every original is in this folder tree, unmodified. "
        "Files holding more than one document are under "
        f"<code>{BUNDLES_DIR}/</code> with their own index, because splitting an "
        "original would mean changing it. <code>documents.json</code> holds this "
        "same metadata for a machine.</p>"
        "<p class='note'><strong>This page needs nothing but a browser.</strong> "
        "Bindery does not have to exist, or run, for it to work.</p>"
        "<table><thead><tr><th>Date</th><th>Document</th><th>From</th>"
        "<th>Type</th><th>Tags</th><th>Pages</th></tr></thead><tbody>"
        + "\n".join(rows)
        + "</tbody></table>"
    )
    return _page(title, body)


def bundle_index_html(entries: list[_Entry], original_name: str) -> str:
    """What lives on which pages of one scanned bundle (REQ-043)."""
    rows = [
        "<tr>"
        f"<td class='mono'>{_page_range(entry)}</td>"
        f"<td>{html.escape(entry.document['title'] or 'Untitled')}</td>"
        f"<td>{html.escape(entry.document['correspondent'] or '')}</td>"
        f"<td class='mono'>{html.escape(entry.document['document_date'] or '—')}</td>"
        "</tr>"
        for entry in sorted(entries, key=lambda e: e.document["page_start"])
    ]
    body = (
        "<h1>Bundle contents</h1>"
        f"<p class='note'>This scan holds {len(entries)} documents. The original is "
        f"<a href='{html.escape(original_name)}'>{html.escape(original_name)}</a> — "
        "it has not been split, because originals are never modified. Open it and "
        "go to the page numbers below.</p>"
        "<table><thead><tr><th>Pages</th><th>Document</th><th>From</th>"
        "<th>Date</th></tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
    )
    return _page("Bundle contents", body)


README = """This is a complete export of a Bindery archive.

  index.html      Open this in any browser. No server, no software required.
  <Correspondent>/<Year>/
                  Every original document, byte-for-byte unmodified, filed by
                  who it came from and when.
  _bundles/       Scans that hold more than one document. Each has an index.html
                  saying what is on which page, next to the untouched original.
  documents.json  The same metadata, for a machine.

Nothing here depends on Bindery. If the software is gone, the papers are still
here and still readable.
"""


def _copy_originals(
    by_file: dict[str, list[_Entry]], destination: Path
) -> tuple[int, int, list[str]]:
    copied = 0
    total_bytes = 0
    missing: list[str] = []
    for sha, group in by_file.items():
        blob = blob_path(sha)
        target = destination / (group[0].relative_path or sha)
        if not blob.is_file():
            # An original we no longer have is an integrity failure, and the
            # export says so out loud rather than quietly omitting it.
            log.error("export: blob missing for %s", sha)
            missing.append(sha)
            for entry in group:
                entry.document["missing_original"] = True
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(blob, target)
        copied += 1
        total_bytes += target.stat().st_size

        if len(group) > 1:
            (target.parent / "index.html").write_text(
                bundle_index_html(group, target.name)
            )
    return copied, total_bytes, missing


async def full_export(
    session: AsyncSession,
    library_ids: list[uuid.UUID],
    *,
    name: str = "bindery-export",
    destination: Path | None = None,
) -> ExportResult:
    """Originals in a semantic tree, metadata, and a static index (REQ-093)."""
    entries = await collect(session, library_ids)
    by_file = plan_layout(entries)

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    root = destination or (export_root() / f"{name}-{stamp}")
    root.mkdir(parents=True, exist_ok=True)

    copied, total_bytes, missing = _copy_originals(by_file, root)

    (root / "documents.json").write_text(
        json.dumps(
            {
                "exported_at": datetime.now(UTC).isoformat(),
                "document_count": len(entries),
                "documents": [
                    {**e.document, "path": e.relative_path} for e in entries
                ],
            },
            indent=2,
        )
    )
    (root / "index.html").write_text(index_html(entries, "Bindery archive"))
    (root / "README.txt").write_text(README)

    log.info(
        "exported %s documents in %s files to %s (%s missing)",
        len(entries), copied, root, len(missing),
    )
    return ExportResult(
        path=root,
        document_count=len(entries),
        file_count=copied,
        byte_size=total_bytes,
        missing_blobs=missing,
    )


async def correspondent_packet(
    session: AsyncSession,
    library_ids: list[uuid.UUID],
    correspondent_id: uuid.UUID,
    name: str,
) -> ExportResult:
    """One correspondent's documents, for handing to an accountant or a lawyer."""
    entries = await collect(session, library_ids, correspondent_id=correspondent_id)
    by_file = plan_layout(entries)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    root = export_root() / f"packet-{safe_component(name, 'packet')}-{stamp}"
    root.mkdir(parents=True, exist_ok=True)

    copied, total_bytes, missing = _copy_originals(by_file, root)
    (root / "index.html").write_text(index_html(entries, f"{name} — documents"))
    (root / "documents.json").write_text(
        json.dumps([{**e.document, "path": e.relative_path} for e in entries], indent=2)
    )
    return ExportResult(
        path=root, document_count=len(entries), file_count=copied,
        byte_size=total_bytes, missing_blobs=missing,
    )


async def go_bag(
    session: AsyncSession,
    library_ids: list[uuid.UUID],
    passphrase: str,
    *,
    destination: Path | None = None,
) -> ExportResult:
    """The vital tier only, encrypted, small enough to carry (REQ-092).

    Encrypted because the whole point is that it *leaves the house* — a USB stick
    in a go-bag, a copy at a relative's. AES-256 in a standard ZIP so it opens in
    7-Zip or Keka, for the same reason the full export is plain HTML: the
    recipient may not have Bindery, and may not want it.
    """
    if len(passphrase or "") < 12:
        raise ValueError("a go-bag passphrase must be at least 12 characters")

    try:
        import pyzipper
    except ImportError:  # pragma: no cover - dependency is declared
        # Refuse rather than quietly writing a plaintext archive of exactly the
        # documents you least want lying around.
        raise RuntimeError(
            "pyzipper is not installed; an unencrypted go-bag is worse than none"
        ) from None

    entries = await collect(session, library_ids, vital_only=True)
    by_file = plan_layout(entries)

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    target = destination or (export_root() / f"go-bag-{stamp}.zip")
    target.parent.mkdir(parents=True, exist_ok=True)

    total_bytes = 0
    written = 0
    missing: list[str] = []
    with pyzipper.AESZipFile(
        target, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES
    ) as archive:
        archive.setpassword(passphrase.encode())
        for sha, group in by_file.items():
            blob = blob_path(sha)
            if not blob.is_file():
                missing.append(sha)
                continue
            archive.write(blob, group[0].relative_path or sha)
            written += 1
            total_bytes += blob.stat().st_size
            if len(group) > 1:
                archive.writestr(
                    f"{Path(group[0].relative_path).parent}/index.html",
                    bundle_index_html(group, Path(group[0].relative_path).name),
                )
        archive.writestr(
            "documents.json",
            json.dumps([{**e.document, "path": e.relative_path} for e in entries], indent=2),
        )
        archive.writestr("index.html", index_html(entries, "Bindery — vital records"))
        archive.writestr("README.txt", README)

    log.info("go-bag: %s vital documents encrypted into %s", len(entries), target)
    return ExportResult(
        path=target, document_count=len(entries), file_count=written,
        byte_size=total_bytes, encrypted=True, missing_blobs=missing,
    )
