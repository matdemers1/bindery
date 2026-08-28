"""T-3.9 — the auto-file gate (REQ-057, REQ-059). Where the kill criterion lives.

The property that matters most: **the model's self-reported confidence never
changes the decision.** It is poorly calibrated, and gating on it would mis-file
in exactly the way that erodes trust.
"""

import pytest

from worker.ai.provider import ClassificationResult, Evidence, TaxonomyChoice, TaxonomyChoices
from worker.classify.gate import (
    GateDecision,
    GateInputs,
    collect,
    date_is_labelled,
    decide,
    replay,
)
from worker.classify.resolve import Resolution


def result(**kwargs) -> ClassificationResult:
    base = {
        "title": "GEICO - Declarations - 4417",
        "summary": "Auto policy declarations page.",
        "document_date": "2026-01-01",
        "correspondent": TaxonomyChoice(existing_id=None, new_name=None),
        "document_type": TaxonomyChoice(),
        "tags": TaxonomyChoices(),
        "confidence": {},
        "evidence": [],
    }
    return ClassificationResult(**{**base, **kwargs})


# --------------------------------------------------------------------------
# The decision itself
# --------------------------------------------------------------------------


def test_a_known_form_match_files_on_its_own() -> None:
    """Deterministic fingerprint. Not an opinion."""
    verdict = decide(GateInputs(known_form_match=True))
    assert verdict.decision is GateDecision.FILED
    assert "known form" in verdict.reasons[0]


def test_a_rule_you_wrote_files_on_its_own() -> None:
    verdict = decide(GateInputs(rule_fired=True))
    assert verdict.decision is GateDecision.FILED


def test_two_strong_signals_file() -> None:
    verdict = decide(GateInputs(all_tags_existing=True, correspondent_existing=True))
    assert verdict.decision is GateDecision.FILED


def test_one_strong_signal_alone_does_not_file() -> None:
    assert decide(GateInputs(correspondent_existing=True)).decision is GateDecision.NEEDS_REVIEW


def test_a_close_neighbour_alone_does_not_file() -> None:
    verdict = decide(GateInputs(neighbour_similarity=0.95))
    assert verdict.decision is GateDecision.NEEDS_REVIEW


def test_a_strong_signal_plus_a_close_neighbour_still_does_not_file() -> None:
    """1.5 < 2.0. Moderate signals assist; they do not decide."""
    verdict = decide(
        GateInputs(correspondent_existing=True, neighbour_similarity=0.9)
    )
    assert verdict.decision is GateDecision.NEEDS_REVIEW


def test_nothing_at_all_goes_to_review_with_an_explanation() -> None:
    verdict = decide(GateInputs())
    assert verdict.decision is GateDecision.NEEDS_REVIEW
    assert verdict.reasons == ["no structural signal was strong enough to file unattended"]


# --------------------------------------------------------------------------
# The property the whole design rests on
# --------------------------------------------------------------------------


@pytest.mark.parametrize("confidence", [0.0, 0.5, 0.99, 1.0])
def test_model_confidence_never_changes_the_decision(confidence) -> None:
    """LLM confidence is displayed (REQ-065), never decisive (REQ-057)."""
    signals = GateInputs(
        correspondent_existing=True,
        model_confidence={"title": confidence, "document_date": confidence},
    )
    assert decide(signals).decision is GateDecision.NEEDS_REVIEW

    signals.known_form_match = True
    assert decide(signals).decision is GateDecision.FILED


def test_a_perfectly_confident_model_with_no_structural_signal_is_reviewed() -> None:
    """The single most important test in this phase."""
    signals = GateInputs(
        model_confidence={field: 1.0 for field in ("title", "correspondent", "document_date")}
    )
    assert decide(signals).decision is GateDecision.NEEDS_REVIEW


def test_an_id_the_model_invented_blocks_filing_outright() -> None:
    """An id that does not exist here means the response referenced something it
    was never shown. Nothing in it is trustworthy enough to file unattended."""
    signals = GateInputs(
        known_form_match=True,
        rule_fired=True,
        all_tags_existing=True,
        correspondent_existing=True,
        rejected_id_count=1,
    )
    verdict = decide(signals)
    assert verdict.decision is GateDecision.NEEDS_REVIEW
    assert "do not exist in this library" in verdict.reasons[0]


# --------------------------------------------------------------------------
# Reproducibility (REQ-057)
# --------------------------------------------------------------------------


def test_a_decision_replays_identically_from_stored_facts() -> None:
    """No model call, no clock, no randomness — just the stored signals."""
    signals = GateInputs(
        all_tags_existing=True,
        correspondent_existing=True,
        neighbour_similarity=0.71,
        model_confidence={"title": 0.4},
    )
    original = decide(signals)
    replayed = replay(signals.to_json())

    assert (replayed.decision, replayed.score, replayed.reasons) == (
        original.decision, original.score, original.reasons
    )


def test_replay_ignores_fields_it_does_not_know() -> None:
    """A stored decision from an older build must still re-derive."""
    stored = GateInputs(known_form_match=True).to_json()
    stored["some_future_signal"] = True
    assert replay(stored).decision is GateDecision.FILED


# --------------------------------------------------------------------------
# Date labelling
# --------------------------------------------------------------------------


def test_a_labelled_date_with_matching_evidence_counts() -> None:
    assert date_is_labelled(
        result(
            document_date="2014-08-11",
            evidence=[
                Evidence(
                    field="document_date",
                    page=1,
                    snippet="Separation Date This Period ..... 2014-08-11",
                )
            ],
        )
    )


def test_a_date_with_no_evidence_does_not_count() -> None:
    assert not date_is_labelled(result(document_date="2014-08-11", evidence=[]))


def test_evidence_that_does_not_contain_the_date_does_not_count() -> None:
    """A citation that would not survive being clicked is not evidence."""
    assert not date_is_labelled(
        result(
            document_date="2014-08-11",
            evidence=[
                Evidence(field="document_date", page=1, snippet="Date of separation shown above")
            ],
        )
    )


def test_an_unlabelled_date_does_not_count() -> None:
    """A date lifted from a letterhead is not an explicitly labelled field."""
    assert not date_is_labelled(
        result(
            document_date="2026-01-01",
            evidence=[Evidence(field="document_date", page=1, snippet="GEICO 2026-01-01")],
        )
    )


def test_a_date_printed_differently_from_iso_still_counts_when_labelled() -> None:
    assert date_is_labelled(
        result(
            document_date="2026-03-04",
            evidence=[
                Evidence(field="document_date", page=1, snippet="Statement date: March 4, 2026")
            ],
        )
    )


def test_no_date_at_all_is_not_a_labelled_date() -> None:
    assert not date_is_labelled(result(document_date=None))


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------


def test_collect_records_confidence_without_acting_on_it() -> None:
    signals = collect(
        result(confidence={"title": 0.99}),
        Resolution(tag_ids=[], all_tags_existing=True),
        known_form_match=False,
        rule_fired=False,
        neighbour_similarity=None,
    )
    assert signals.model_confidence == {"title": 0.99}
    # No tags means "all tags existing" is vacuous, not a signal.
    assert signals.all_tags_existing is False
