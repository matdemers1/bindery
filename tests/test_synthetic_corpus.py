"""The synthetic OCR corpus's guard rails (BND-T-005).

The OCR itself only runs in the worker image (`test_ocr_accuracy.py`, `-m slow`).
These checks are about the *fixtures*, so they run in the fast tier where a
mistake is caught in seconds.

The one that matters most: this repository is public, and the corpus sits a
directory away from where the maintainer stages real records. Everything in it
must be visibly invented, so a real number pasted in by habit fails here rather
than being published.
"""

import json
import re
from pathlib import Path

from tests.synthetic_corpus import FIXTURES

BASELINE = Path(__file__).parent / "synthetic_corpus_baseline.json"


def _all_text() -> str:
    return "\n".join(fixture.expected for fixture in FIXTURES)


def test_fixtures_are_named_once_and_carry_text() -> None:
    names = [fixture.name for fixture in FIXTURES]
    assert len(names) == len(set(names)), "two synthetic fixtures share a name"
    for fixture in FIXTURES:
        assert len(fixture.expected.split()) >= 40, f"{fixture.name} is too short to score"
        assert fixture.why, f"{fixture.name} does not say what it is standing in for"


def test_the_baseline_covers_exactly_the_fixtures() -> None:
    baseline = json.loads(BASELINE.read_text())["errors"]
    assert sorted(baseline) == sorted(fixture.name for fixture in FIXTURES)
    assert all(isinstance(errors, int) and errors >= 0 for errors in baseline.values())


def test_every_identifier_is_in_a_range_that_cannot_be_real() -> None:
    """SSNs in area 000, phone numbers in 555-01xx, ZIP 04000 — none is issued."""
    text = _all_text()
    for ssn in re.findall(r"\b\d{3}-\d{2}-\d{4}\b", text):
        assert ssn.startswith("000-"), f"{ssn} is shaped like an SSN outside area 000"
    for match in re.finditer(r"(\w+ )?\b(\d{3}-\d{3}-\d{4})\b", text):
        if match.group(1) == "Parcel ":
            continue  # a tax-map parcel number, not a telephone
        phone = match.group(2)
        assert phone[4:9] == "555-0", f"{phone} is outside the 555-01xx fictional range"
    for zip_code in re.findall(r"\b[A-Z]{2} (\d{5})\b", text):
        assert zip_code == "04000", f"ZIP {zip_code} may be a real one"


def test_the_people_are_invented() -> None:
    """The cast is Example, Sample and Placeholder — no real surname belongs here."""
    surnames = set(re.findall(r"\b(?:Jordan A\.?|Morgan|Casey|Taylor|R\.) (\w+)", _all_text()))
    assert surnames <= {"Example", "Sample", "Placeholder"}, surnames
