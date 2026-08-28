"""Shared shapes for the boundary proposers."""

import re
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Boundary:
    """A vote that a new document starts at `page`.

    `weight` is a confidence contribution, not a probability. Weights from
    different proposers are summed and compared against a threshold, so the
    scale only has to be consistent with itself.
    """

    page: int
    weight: float
    reason: str


@dataclass
class PageFeatures:
    """Everything the proposers need about one page, computed once.

    Derived from the word boxes rather than the plain text, because layout — how
    many lines, how wide, where the left margin sits — is what distinguishes a
    continuation sheet from the first page of something new.
    """

    number: int
    text: str
    word_count: int
    line_count: int
    mean_line_length: float
    left_margin: float
    top_ink: float
    page_width: float
    page_height: float
    footer_text: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_blank(self) -> bool:
        # Not zero: a scanner's back-of-page bleed-through and a stray staple
        # shadow both produce a handful of spurious "words".
        return self.word_count <= 3


Proposer = Callable[[list[PageFeatures]], list[Boundary]]

FOOTER_FRACTION = 0.15
_WHITESPACE = re.compile(r"\s+")


def features_from_boxes(page: dict[str, Any], text: str) -> PageFeatures:
    words = [word for line in page.get("lines", []) for word in line["words"]]
    lines = page.get("lines", [])
    height = float(page.get("height") or 0.0)
    width = float(page.get("width") or 0.0)

    line_lengths = [
        sum(len(word["t"]) for word in line["words"]) for line in lines if line["words"]
    ]
    left_edges = [
        min(word["x0"] for word in line["words"]) for line in lines if line["words"]
    ]

    footer_cutoff = height * (1 - FOOTER_FRACTION) if height else None
    footer_words = (
        [word["t"] for word in words if word["y0"] >= footer_cutoff]
        if footer_cutoff is not None
        else []
    )

    return PageFeatures(
        number=int(page.get("number", 0)),
        text=text,
        word_count=len(words),
        line_count=len(lines),
        mean_line_length=statistics.fmean(line_lengths) if line_lengths else 0.0,
        # Median, not mean: a single centred title would drag the mean right and
        # make an ordinary page look like a layout change.
        left_margin=statistics.median(left_edges) if left_edges else 0.0,
        top_ink=min((word["y0"] for word in words), default=0.0),
        page_width=width,
        page_height=height,
        footer_text=_WHITESPACE.sub(" ", " ".join(footer_words)).strip(),
    )
