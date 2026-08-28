"""Cheap deterministic boundary proposers.

> Heuristics propose, Claude confirms.

Each proposer answers one question about the seam between page *k-1* and page
*k*, and returns a weighted vote with a human-readable reason. Nothing here is
load-bearing: the manual editor ships regardless (REQ-036), and Phase 3 adds an
LLM pass over the ambiguous seams (REQ-035). The point of doing the cheap work
first is that the model only ever sees the cases the cheap work could not settle.

A proposer that is unsure returns nothing. Silence is the correct output for
most seams in most files — the common case is a scan that is one document.
"""

from worker.segment.heuristics.base import (
    Boundary,
    PageFeatures,
    Proposer,
    features_from_boxes,
)
from worker.segment.heuristics.blank_page import propose_blank_page
from worker.segment.heuristics.form_number import propose_form_number
from worker.segment.heuristics.layout_shift import propose_layout_shift
from worker.segment.heuristics.page_number_reset import propose_page_number_reset

PROPOSERS: list[Proposer] = [
    propose_blank_page,
    propose_page_number_reset,
    propose_form_number,
    propose_layout_shift,
]

__all__ = [
    "PROPOSERS",
    "Boundary",
    "PageFeatures",
    "Proposer",
    "features_from_boxes",
    "propose_blank_page",
    "propose_form_number",
    "propose_layout_shift",
    "propose_page_number_reset",
]
