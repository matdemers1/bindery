"""The semantic folder tree (T-6.4, REQ-094, REQ-043).

Both the mirror and the full export lay documents out the same way, because
they answer the same question: *if Bindery is gone, can a person still find
this?* A folder tree is the most durable index there is — every operating
system made in the last forty years can browse one.

The shape:

    Correspondent/2019/2019-04-12 - Award Letter.pdf
    _bundles/2019-04-12 scan (12 pages)/
        index.html
        pages 1-3 - DD-214.txt
        ...

Whole-file documents get a real file. **Bundles cannot** — one PDF holding
twelve documents can't be twelve files without splitting the original, and
originals are never modified. So a bundle gets a directory holding a readable
index that says what lives on which pages, next to a link to the one original.
That asymmetry is the honest representation of the underlying invariant.

Nothing here is a source of truth. The tree is disposable and rebuildable from
the database; deleting it loses nothing.
"""

import re
import unicodedata

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SPACES = re.compile(r"\s+")

# Reserved on Windows, and an exported archive should survive being copied onto
# a Windows machine or a FAT-formatted USB stick.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_component(value: str | None, fallback: str = "Untitled") -> str:
    """One path component that is safe on macOS, Linux, Windows and FAT."""
    text = unicodedata.normalize("NFC", (value or "").strip())
    text = _UNSAFE.sub("-", text)
    text = _SPACES.sub(" ", text).strip(" .")
    if text.upper().split(".")[0] in _RESERVED:
        text = f"_{text}"
    if not text:
        text = fallback
    # 100 leaves room for a disambiguating suffix inside the common 255-byte
    # filename limit even when characters are multi-byte.
    return text[:100].strip() or fallback


def document_folder(correspondent: str | None, year: int | None) -> tuple[str, ...]:
    """`Correspondent/Year`, with honest placeholders rather than guesses."""
    return (
        safe_component(correspondent, "Unfiled"),
        str(year) if year else "Undated",
    )


def document_filename(
    *, title: str | None, document_date: str | None, extension: str
) -> str:
    """`YYYY-MM-DD - Title.ext`, so a plain alphabetical sort is chronological."""
    stem = safe_component(title, "Untitled")
    prefix = f"{document_date} - " if document_date else ""
    suffix = extension if extension.startswith(".") else f".{extension}"
    return f"{safe_component(prefix + stem, 'Untitled')}{suffix}"


def bundle_folder(*, original_filename: str | None, received: str, pages: int) -> str:
    """A directory name for a source file that holds more than one document."""
    stem = safe_component((original_filename or "scan").rsplit(".", 1)[0], "scan")
    return safe_component(f"{received} {stem} ({pages} pages)")


def deduplicate(name: str, taken: set[str]) -> str:
    """Two award letters from the VA in the same year must both survive.

    Silently overwriting one with the other would be exactly the kind of quiet
    data loss this whole system exists to prevent, so collisions get a counter.
    """
    if name not in taken:
        taken.add(name)
        return name
    stem, dot, extension = name.rpartition(".")
    if not dot:
        stem, extension = name, ""
    counter = 2
    while True:
        candidate = f"{stem} ({counter}){dot}{extension}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
        counter += 1
