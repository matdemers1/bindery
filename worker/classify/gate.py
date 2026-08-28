"""The auto-file gate (REQ-057, REQ-059). Where the kill criterion lives.

> LLM self-reported confidence is **poorly calibrated** — 0.9 does not mean 90%
> correct. Gating auto-filing on it would mis-file in a way that erodes trust,
> which is precisely the kill criterion.

So the gate never reads the model's opinion of itself. It reads **facts about
the result**: did a deterministic fingerprint match, did a rule the user wrote
fire, did the taxonomy come back entirely from ids that already existed, did the
model quote a page sentence that actually contains the date it returned.

Every input is recorded on the classification row, and the decision is a pure
function of them — so replaying a gate decision needs no model call and produces
the same answer years later (REQ-057). If this function changes, old decisions
can be re-derived and compared rather than merely trusted.

The model's confidence is still captured and shown to the user (REQ-065). It is
information, not authority.
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from worker.ai.provider import ClassificationResult
from worker.classify.resolve import Resolution


class GateDecision(StrEnum):
    FILED = "filed"
    NEEDS_REVIEW = "needs_review"


# Weights, not probabilities. A decisive signal clears the bar alone; two strong
# signals agreeing also clear it; nothing weaker does.
DECISIVE = 2.0
STRONG = 1.0
MODERATE = 0.5
THRESHOLD = 2.0

# A neighbour this close is structurally the same kind of document as one that
# was already filed correctly.
SIMILARITY_THRESHOLD = 0.60

# Words that mark a date as *labelled* rather than inferred from a letterhead.
DATE_LABELS = re.compile(
    r"\b(date|dated|issued|effective|statement|separation|expires?|expiry|"
    r"due|period|as of|on or about)\b",
    re.IGNORECASE,
)


@dataclass
class GateInputs:
    """The facts. Serialised verbatim onto the classification row."""

    known_form_match: bool = False
    rule_fired: bool = False
    all_tags_existing: bool = False
    correspondent_existing: bool = False
    document_type_existing: bool = False
    date_is_labelled: bool = False
    has_date: bool = False
    neighbour_similarity: float | None = None
    tag_count: int = 0
    invented_count: int = 0
    rejected_id_count: int = 0
    # Recorded for the UI and for calibration. Never read by the decision.
    model_confidence: dict[str, float] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "known_form_match": self.known_form_match,
            "rule_fired": self.rule_fired,
            "all_tags_existing": self.all_tags_existing,
            "correspondent_existing": self.correspondent_existing,
            "document_type_existing": self.document_type_existing,
            "date_is_labelled": self.date_is_labelled,
            "has_date": self.has_date,
            "neighbour_similarity": self.neighbour_similarity,
            "tag_count": self.tag_count,
            "invented_count": self.invented_count,
            "rejected_id_count": self.rejected_id_count,
            "model_confidence": self.model_confidence,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "GateInputs":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in payload.items() if key in known})


@dataclass(frozen=True)
class GateResult:
    decision: GateDecision
    score: float
    reasons: list[str]


MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_WORD = re.compile(r"[a-z]+|\d+")


def snippet_contains_date(snippet: str, iso_date: str) -> bool:
    """Does this text actually carry that date, however the page printed it?

    Pages print "March 4, 2026", "03/04/2026" and "2026-03-04" for the same day,
    so a literal ISO comparison would reject almost every real citation. Year,
    month and day are matched independently — month by number or by name — which
    is permissive about *format* while staying strict about the thing that
    matters: the snippet has to contain the date it is offered as evidence for.
    """
    try:
        year, month, day = (int(part) for part in iso_date.split("-"))
    except (ValueError, TypeError):
        return False

    numbers: set[int] = set()
    months: set[int] = set()
    for token in _WORD.findall(snippet.casefold()):
        if token.isdigit():
            numbers.add(int(token))
        elif len(token) >= 3 and token[:3] in MONTHS:
            months.add(MONTHS[token[:3]])

    return (
        year in numbers
        and (month in numbers or month in months)
        and day in numbers
    )


def date_is_labelled(result: ClassificationResult) -> bool:
    """Did the model quote a *labelled* sentence that carries the date it returned?

    Two things have to hold: the evidence for `document_date` contains the date,
    and it contains a word that marks the date as declared rather than incidental.
    A date lifted from a letterhead has the second but not the first framing; a
    snippet that does not contain the date at all is not evidence — it is a
    citation that would not survive being clicked in the why-panel.
    """
    if not result.document_date:
        return False
    return any(
        evidence.field == "document_date"
        and snippet_contains_date(evidence.snippet or "", result.document_date)
        and DATE_LABELS.search(evidence.snippet or "") is not None
        for evidence in result.evidence
    )


def collect(
    result: ClassificationResult,
    resolution: Resolution,
    *,
    known_form_match: bool,
    rule_fired: bool,
    neighbour_similarity: float | None,
) -> GateInputs:
    return GateInputs(
        known_form_match=known_form_match,
        rule_fired=rule_fired,
        all_tags_existing=resolution.all_tags_existing and bool(resolution.tag_ids),
        correspondent_existing=resolution.correspondent_was_existing,
        document_type_existing=resolution.document_type_was_existing,
        date_is_labelled=date_is_labelled(result),
        has_date=bool(result.document_date),
        neighbour_similarity=neighbour_similarity,
        tag_count=len(resolution.tag_ids),
        invented_count=len(resolution.invented_names),
        rejected_id_count=len(resolution.rejected_ids),
        model_confidence=dict(result.confidence),
    )


def decide(inputs: GateInputs) -> GateResult:
    """A pure function of stored facts. No model call, no clock, no randomness."""
    score = 0.0
    reasons: list[str] = []

    if inputs.known_form_match:
        score += DECISIVE
        reasons.append("matched a known form by deterministic fingerprint")
    if inputs.rule_fired:
        score += DECISIVE
        reasons.append("a rule you wrote applied to this document")

    if inputs.all_tags_existing:
        score += STRONG
        reasons.append("every tag came from the archive's existing taxonomy")
    if inputs.correspondent_existing:
        score += STRONG
        reasons.append("the correspondent was already in the archive")
    if inputs.date_is_labelled:
        score += STRONG
        reasons.append("the date came from an explicitly labelled field")

    if inputs.neighbour_similarity is not None and (
        inputs.neighbour_similarity >= SIMILARITY_THRESHOLD
    ):
        score += MODERATE
        reasons.append(
            f"closely resembles a document already filed "
            f"({inputs.neighbour_similarity:.0%} similar)"
        )

    # An id the model returned that does not exist in this library is not a
    # near-miss — it means the response referenced something it was never shown,
    # and nothing in it should be trusted enough to file unattended.
    if inputs.rejected_id_count:
        score = 0.0
        reasons = [
            f"{inputs.rejected_id_count} returned id(s) do not exist in this library"
        ]

    decision = GateDecision.FILED if score >= THRESHOLD else GateDecision.NEEDS_REVIEW
    if decision is GateDecision.NEEDS_REVIEW and not reasons:
        reasons.append("no structural signal was strong enough to file unattended")
    return GateResult(decision=decision, score=score, reasons=reasons)


def replay(stored_signals: dict[str, Any]) -> GateResult:
    """Re-derive a decision from what was stored. REQ-057, made executable."""
    return decide(GateInputs.from_json(stored_signals))
