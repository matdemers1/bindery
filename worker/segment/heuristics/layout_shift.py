"""An abrupt change in page geometry suggests a different document.

The weakest of the four proposers, and deliberately so: a chart, a signature
page, or a form's continuation sheet all shift the layout without starting
anything new. It votes lightly, and is meant to tip a seam that another
proposer already suspects rather than to carry one alone.

Compared features are all normalised to the page, so a mixed-size bundle
(letter and legal in one scan) does not read as a shift on every seam.
"""

import itertools

from worker.segment.heuristics.base import Boundary, PageFeatures

WEIGHT = 0.4
# Fraction-of-page thresholds, tuned to fire on "obviously different", not
# "slightly different".
MARGIN_SHIFT = 0.12
LINE_COUNT_RATIO = 2.2
LINE_LENGTH_RATIO = 2.2
MIN_LINES = 4


def _ratio(a: float, b: float) -> float:
    low, high = sorted((abs(a), abs(b)))
    return high / low if low > 0.5 else float("inf") if high > 0.5 else 1.0


def propose_layout_shift(pages: list[PageFeatures]) -> list[Boundary]:
    boundaries: list[Boundary] = []

    for previous, current in itertools.pairwise(pages):
        if previous.is_blank or current.is_blank:
            continue  # the blank-page proposer owns this seam
        if previous.line_count < MIN_LINES or current.line_count < MIN_LINES:
            continue  # too little ink to say anything about layout

        reasons: list[str] = []

        width = previous.page_width or current.page_width or 1.0
        margin_delta = abs(previous.left_margin - current.left_margin) / width
        if margin_delta > MARGIN_SHIFT:
            reasons.append(f"left margin moves {margin_delta:.0%} of the page width")

        if _ratio(previous.line_count, current.line_count) > LINE_COUNT_RATIO:
            reasons.append(
                f"line count changes from {previous.line_count} to {current.line_count}"
            )

        if _ratio(previous.mean_line_length, current.mean_line_length) > LINE_LENGTH_RATIO:
            reasons.append("mean line length changes sharply")

        # One signal is noise; two agreeing is a shift worth a light vote.
        if len(reasons) >= 2:
            boundaries.append(
                Boundary(
                    page=current.number,
                    weight=WEIGHT,
                    reason="layout shift — " + "; ".join(reasons),
                )
            )

    return boundaries
