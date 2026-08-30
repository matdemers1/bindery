"""The golden corpus's own guard rails (T-9.5, REQ-018).

Separate from `test_ocr_accuracy.py`, which is skipped entirely when the OCR
toolchain is absent — and these checks are about the *fixtures*, not about OCR,
so hiding them behind that skip meant they never ran anywhere.
"""

import pytest

from tests.ocr_scoring import UNVERIFIED_MARKER, UnverifiedFixture, score


def test_a_fixture_still_holding_its_staged_ocr_is_refused() -> None:
    """Scoring OCR against its own output returns 100% and measures nothing.

    `scripts/stage-corpus-fixture.py` pre-fills `expected.txt` with what OCR
    currently reads, so correcting a page takes minutes rather than the hour
    that typing one from nothing takes — which is why the R-01 figure has been
    outstanding since Phase 1. The marker is what stops the shortcut becoming
    the answer.
    """
    with pytest.raises(UnverifiedFixture):
        score("dd214", f"{UNVERIFIED_MARKER} — check me\nSOME TEXT", "SOME TEXT")


def test_a_corrected_fixture_scores_normally() -> None:
    assert score("dd214", "SOME TEXT", "SOME TEXT").accuracy == 1.0
    assert score("dd214", "SOME TEXT HERE", "SOME TEXT").accuracy < 1.0
