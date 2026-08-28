"""Per-word coordinates, extracted from a PDF's text layer.

`pdftotext -bbox-layout` (poppler, already in the worker image) emits XHTML with
one `<word>` element per word carrying its bounding box, grouped into lines and
blocks. That gives us three things in a single pass:

- the highlight overlay's rectangles (REQ-015),
- per-page text with line structure preserved, which is what makes `ts_headline`
  snippets readable,
- a page count that reflects the normalized PDF rather than the original.

Deliberately not PyMuPDF: poppler is already installed, and this avoids adding an
AGPL dependency to a project whose export path is meant to outlive it.

Coordinates are in PDF points, with the origin at the top-left of the page, and
each page carries its own width and height so the viewer can scale boxes onto a
render at any DPI.
"""

from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from worker import subprocess_util

EXTRACT_TIMEOUT_SECONDS = 600
XHTML = "{http://www.w3.org/1999/xhtml}"


def _attr_float(element: ElementTree.Element, name: str) -> float:
    return round(float(element.get(name, 0.0)), 2)


def parse_bbox_xhtml(document: bytes) -> dict[str, Any]:
    """Turn `pdftotext -bbox-layout` output into the ocr.json structure."""
    root = ElementTree.fromstring(document)
    pages: list[dict[str, Any]] = []

    for index, page in enumerate(root.iter(f"{XHTML}page"), start=1):
        lines: list[dict[str, Any]] = []
        for line in page.iter(f"{XHTML}line"):
            words = [
                {
                    "x0": _attr_float(word, "xMin"),
                    "y0": _attr_float(word, "yMin"),
                    "x1": _attr_float(word, "xMax"),
                    "y1": _attr_float(word, "yMax"),
                    "t": (word.text or "").strip(),
                }
                for word in line.iter(f"{XHTML}word")
                if (word.text or "").strip()
            ]
            if words:
                lines.append({"words": words})

        pages.append(
            {
                "number": index,
                "width": _attr_float(page, "width"),
                "height": _attr_float(page, "height"),
                "lines": lines,
            }
        )

    return {"generator": "pdftotext -bbox-layout", "pages": pages}


def page_text(page: dict[str, Any]) -> str:
    """Rebuild readable text for one page, one line per line.

    Line structure is kept because a snippet that runs every line together is
    much harder to read at a glance, and glanceability is the whole point.
    """
    return "\n".join(
        " ".join(word["t"] for word in line["words"]) for line in page["lines"]
    )


async def extract_word_boxes(pdf: Path) -> dict[str, Any]:
    _, stdout, _ = await subprocess_util.run(
        ["pdftotext", "-bbox-layout", str(pdf), "-"],
        timeout=EXTRACT_TIMEOUT_SECONDS,
    )
    return parse_bbox_xhtml(stdout)
