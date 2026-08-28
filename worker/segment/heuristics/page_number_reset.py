"""A footer that counts back to 1 is a new document.

"Page 4 of 12" followed by "Page 1 of 3" is about as unambiguous as bundle
segmentation gets. A bare trailing integer resetting is weaker evidence — it
could be a numbered exhibit list — so it votes with less weight.
"""

import itertools
import re

from worker.segment.heuristics.base import Boundary, PageFeatures

X_OF_Y = re.compile(r"\bpage\s+(\d{1,4})\s+of\s+(\d{1,4})\b", re.IGNORECASE)
BARE = re.compile(r"(?:^|\s)(\d{1,3})\s*$")

STRONG_WEIGHT = 1.0
WEAK_WEIGHT = 0.5


def _parsed(page: PageFeatures) -> tuple[int, int] | None:
    match = X_OF_Y.search(page.footer_text) or X_OF_Y.search(page.text)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def _bare_number(page: PageFeatures) -> int | None:
    match = BARE.search(page.footer_text)
    return int(match.group(1)) if match else None


def propose_page_number_reset(pages: list[PageFeatures]) -> list[Boundary]:
    boundaries: list[Boundary] = []

    for previous, current in itertools.pairwise(pages):
        previous_parsed, current_parsed = _parsed(previous), _parsed(current)

        if previous_parsed and current_parsed:
            previous_n, previous_total = previous_parsed
            current_n, current_total = current_parsed
            if current_n == 1 and previous_n >= 1 and previous_total != current_total:
                boundaries.append(
                    Boundary(
                        page=current.number,
                        weight=STRONG_WEIGHT,
                        reason=(
                            f"footer resets: page {previous_n} of {previous_total} "
                            f"→ page 1 of {current_total}"
                        ),
                    )
                )
            elif current_n == 1 and previous_n > 1:
                boundaries.append(
                    Boundary(
                        page=current.number,
                        weight=STRONG_WEIGHT,
                        reason=f"footer resets to page 1 after page {previous_n}",
                    )
                )
            continue

        previous_bare, current_bare = _bare_number(previous), _bare_number(current)
        if previous_bare is not None and current_bare == 1 and previous_bare > 1:
            boundaries.append(
                Boundary(
                    page=current.number,
                    weight=WEAK_WEIGHT,
                    reason=f"footer number resets to 1 after {previous_bare}",
                )
            )

    return boundaries
