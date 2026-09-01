"""Derived-artifact paths (Architecture L8).

Everything under `derived/` is **reproducible from a blob**: losing it costs CPU,
never data. That is what lets a stage overwrite its own output on replay while
the original stays untouched (invariant 1).

Artifacts are addressed by the source file's content hash, not its id, so
re-ingesting identical bytes reuses work already done.
"""

from dataclasses import dataclass
from pathlib import Path

from api.config import get_settings


@dataclass(frozen=True)
class DerivedPaths:
    root: Path

    @property
    def normalized_pdf(self) -> Path:
        """OCRmyPDF output: PDF/A, hidden text layer over the original image."""
        return self.root / "normalized.pdf"

    @property
    def ocr_text(self) -> Path:
        """Sidecar plain text for the whole file (REQ-014)."""
        return self.root / "ocr.txt"

    @property
    def word_boxes(self) -> Path:
        """Per-word coordinates, per page. This is the highlight overlay (REQ-015)."""
        return self.root / "ocr.json"

    @property
    def pages_dir(self) -> Path:
        return self.root / "pages"

    @property
    def thumbs_dir(self) -> Path:
        return self.root / "thumbs"

    def page_render(self, page_number: int) -> Path:
        return self.pages_dir / f"{page_number:04d}.webp"

    def page_thumb(self, page_number: int) -> Path:
        return self.thumbs_dir / f"{page_number:04d}.webp"

    def mkdirs(self) -> None:
        for directory in (self.root, self.pages_dir, self.thumbs_dir):
            directory.mkdir(parents=True, exist_ok=True)


def derived_for(sha256: str) -> DerivedPaths:
    return DerivedPaths(get_settings().derived_root / sha256)


def relative_to_data(path: Path) -> str:
    """Store paths relative to DATA_ROOT so the pool can be remounted anywhere."""
    return str(path.relative_to(get_settings().data_root))


def resolve_in_data(relative: str) -> Path:
    """Inverse of `relative_to_data`, with traversal refused.

    Stored paths are used to serve bytes, so a value that escapes DATA_ROOT is
    treated as an attack rather than a typo.
    """
    root = get_settings().data_root.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"path escapes the data root: {relative!r}")
    return candidate


def purge_derived(sha256: str) -> bool:
    """Remove every derived artifact for one original (T-16.5).

    The one place in this application that deletes derived files on purpose,
    and safe for the same reason the mirror is: nothing here is an original.
    Every byte under `derived/<sha>` — the normalised PDF, the page renders,
    the thumbnails — is reproducible from the blob by re-running the pipeline.

    It exists for the vault. Encrypting an original while leaving plaintext
    renders of its pages beside it would hide the document and keep the
    pictures of it, which is not hiding the document.

    Returns whether the directory is gone, so a caller can say so rather than
    assume it.
    """
    import shutil

    directory = derived_for(sha256).root
    if not directory.exists():
        return True
    shutil.rmtree(directory, ignore_errors=True)
    return not directory.exists()
