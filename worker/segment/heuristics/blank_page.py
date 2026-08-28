"""A blank page is a separator sheet.

The most reliable signal in a scanned bundle, because it is usually deliberate:
someone fed a blank sheet between documents, or the duplex scanner emitted the
empty back of a single-sided page.

The boundary goes *after* the blank run, not at it. A trailing blank belongs to
the document that preceded it far more often than it opens the next one.
"""

from worker.segment.heuristics.base import Boundary, PageFeatures

WEIGHT = 1.0


def propose_blank_page(pages: list[PageFeatures]) -> list[Boundary]:
    boundaries: list[Boundary] = []
    for index, page in enumerate(pages):
        if not page.is_blank:
            continue
        following = pages[index + 1] if index + 1 < len(pages) else None
        # A blank last page separates nothing, and a run of blanks produces one
        # boundary rather than one per sheet.
        if following is None or following.is_blank:
            continue
        boundaries.append(
            Boundary(
                page=following.number,
                weight=WEIGHT,
                reason=f"page {page.number} is blank",
            )
        )
    return boundaries
