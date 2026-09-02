"""The mirror tree (T-6.4, REQ-094, REQ-043).

A live, always-current folder tree of the archive, sitting next to the blob
store, browsable over SMB from any machine in the house without opening
Bindery. It is the everyday version of the promise the full export makes for the
end of the world: *your files stay findable in a plain folder*.

Two properties keep it honest:

**It is disposable.** The mirror is derived, never authoritative. Delete it and
`rebuild` recreates it from the database with nothing lost. That is why it can
be blown away and rewritten rather than incrementally patched — reconciling a
tree against a changing database is a synchronisation problem, and rebuilding it
is not.

**It costs almost nothing.** Entries are hardlinks to the content-addressed
blobs, not copies, so a mirror of a 400GB archive occupies directory entries and
little else. Blobs are mode 0444, so a hardlink cannot be used to modify an
original even by accident. Across filesystems, where hardlinks are impossible,
it falls back to copying and says so.
"""

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.export.archive_export import (
    BUNDLES_DIR,
    bundle_index_html,
    collect,
    index_html,
    plan_layout,
)
from api.storage.blobs import blob_path

log = logging.getLogger("bindery.mirror")


def mirror_root() -> Path:
    return get_settings().data_root / "mirror"


@dataclass
class MirrorResult:
    root: Path
    linked: int
    copied: int
    missing: int
    bundles: int
    removed: int


def _link_or_copy(source: Path, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    try:
        os.link(source, target)
        return "linked"
    except OSError:
        # Different filesystem, or a filesystem without hardlinks. Copying is
        # correct, just expensive, and the caller reports the difference.
        import shutil

        shutil.copy2(source, target)
        os.chmod(target, 0o444)
        return "copied"


def _prune(root: Path, keep: set[Path]) -> int:
    """Remove mirror entries no live document maps to.

    This is the one place in Bindery that deletes files, and it is safe for
    exactly one reason: **nothing here is an original.** Every entry is a
    hardlink or a copy of a blob that remains untouched in the blob store, and
    the whole tree is regenerated from the database on the next rebuild.
    REQ-090 is about originals and records; it is not violated by tidying a
    derived index.
    """
    removed = 0
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            if not any(path.iterdir()):
                path.rmdir()
                removed += 1
            continue
        if path not in keep:
            path.unlink()
            removed += 1
    return removed


async def rebuild(
    session: AsyncSession,
    library_ids: list[uuid.UUID],
    *,
    root: Path | None = None,
) -> MirrorResult:
    """Regenerate the whole tree from the database. Idempotent.

    The database reads happen here; the tree is written off the event loop
    (CR-010). A rebuild hardlinks or copies one entry per source file and then
    walks the whole tree to prune it, and all of that used to run inside the
    request handler — so "Rebuild mirror" on the Trust screen froze search,
    login, `/api/live` and the container healthcheck until it finished.
    """
    destination = root or mirror_root()

    entries = await collect(session, library_ids)
    by_file = plan_layout(entries)

    return await asyncio.to_thread(_rebuild_tree, entries, by_file, destination)


def _rebuild_tree(
    entries: list,
    by_file: dict[str, list],
    destination: Path,
) -> MirrorResult:
    """The blocking half of `rebuild` — link, write, prune.

    Moved, not changed: the same loop, the same indexes, the same `_prune`.
    """
    destination.mkdir(parents=True, exist_ok=True)

    linked = copied = missing = bundles = 0
    keep: set[Path] = set()

    for sha, group in by_file.items():
        blob = blob_path(sha)
        target = destination / (group[0].relative_path or sha)
        if not blob.is_file():
            log.error("mirror: blob missing for %s", sha)
            missing += 1
            continue
        outcome = _link_or_copy(blob, target)
        linked += outcome == "linked"
        copied += outcome == "copied"
        keep.add(target)

        if len(group) > 1:
            # A bundle cannot become one file per document without splitting the
            # original, so it becomes a folder that says what is on which page.
            bundle_index = target.parent / "index.html"
            bundle_index.write_text(bundle_index_html(group, target.name))
            keep.add(bundle_index)
            bundles += 1

    top_index = destination / "index.html"
    top_index.write_text(index_html(entries, "Bindery archive (mirror)"))
    keep.add(top_index)

    readme = destination / "README.txt"
    readme.write_text(
        "This folder is a mirror of the Bindery archive, rebuilt from the\n"
        "database. It is safe to delete: nothing here is an original, and the\n"
        "next rebuild recreates it exactly.\n\n"
        f"Scans holding more than one document are under {BUNDLES_DIR}/, each\n"
        "with an index.html saying what is on which page. They are not split,\n"
        "because originals are never modified.\n"
    )
    keep.add(readme)

    removed = _prune(destination, keep)
    log.info(
        "mirror rebuilt: %s linked, %s copied, %s bundles, %s stale entries removed",
        linked, copied, bundles, removed,
    )
    return MirrorResult(
        root=destination, linked=linked, copied=copied,
        missing=missing, bundles=bundles, removed=removed,
    )
