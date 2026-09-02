"""The R-01 gate must never report a pass while measuring nothing (CR-037).

`tests/test_ocr_accuracy.py::test_golden_corpus_word_accuracy` is the gate that
decides whether Phase 3's classification design is built on text good enough to
classify. It has never run. `tests/corpus/` holds a README, an `__init__.py` and
a synthetic-fixture helper, and no real documents at all, so the gate called
`pytest.skip` unconditionally and the CI `integration` job reported green having
measured nothing. That is what let Phase 3 ship with REQ-058 unscored.

**The corpus cannot be fixed here.** The fixtures are the owner's real DD-214,
VA medical records and financial statements; they are why this repository must
never be public, and inventing stand-ins would replace an honest absence with a
dishonest number. What *can* be fixed — and is, by this file — is the reporting:
an unmeasured gate must be loud, and it must be loud in the **fast** tier, where
someone actually reads the output. The gate itself only runs in the worker image
behind `-m slow`, so a defect in how it reports would otherwise be discovered by
nobody.

So this file asserts three things about the gate, structurally, on every run:

1. it cannot be a silent skip again,
2. with no corpus it is a **strict xfail** — reported as a known failure, never
   as a pass, and turning into a hard failure the moment it starts passing,
3. any fixture that *is* staged is complete and hand-corrected, so a half-staged
   corpus fails in seconds rather than after a full OCR run.
"""

from pathlib import Path

from tests import test_ocr_accuracy as gate
from tests.ocr_scoring import UNVERIFIED_MARKER

CORPUS_ROOT = Path(__file__).resolve().parent / "corpus"


def _markers(function) -> list:
    return list(getattr(function, "pytestmark", []))


def _xfail(function):
    return next((mark for mark in _markers(function) if mark.name == "xfail"), None)


def test_the_gate_is_the_test_this_file_thinks_it_is() -> None:
    """A guard that names a function by string outlives the function.

    If the gate is renamed or moved, every assertion below would pass while
    inspecting nothing — which is the exact class of failure this file exists
    to close, so it is closed here first.
    """
    assert callable(getattr(gate, "test_golden_corpus_word_accuracy", None)), (
        "tests/test_ocr_accuracy.py no longer defines "
        "`test_golden_corpus_word_accuracy` — the R-01 gate has been renamed or "
        "removed, and this file is now guarding nothing"
    )
    assert gate.GATE == 0.90, f"the R-01 threshold has moved to {gate.GATE}"
    assert callable(gate.real_fixtures)


def test_an_empty_corpus_is_never_reported_as_a_pass() -> None:
    """The whole finding, in one assertion.

    With no fixtures the gate must fail and be *declared* to fail. `strict=True`
    is what makes the declaration self-retiring: on the day real fixtures land
    and the gate passes, the marker turns the pass into an error until somebody
    removes it, so the xfail cannot become the new permanent green.
    """
    marker = _xfail(gate.test_golden_corpus_word_accuracy)

    if gate.real_fixtures():
        # The good state. The marker may still be declared, but its condition
        # must be false, or a real measurement would be swallowed as "expected
        # to fail" — which would be the same defect with the sign flipped.
        assert marker is None or marker.kwargs.get("condition") is False, (
            "real corpus fixtures are staged and the gate is still marked "
            "xfail, so an actual R-01 measurement is being reported as a known "
            "failure. Remove the marker: the gate can do its job now."
        )
        return

    assert marker is not None, (
        "the R-01 gate has no xfail marker and there is no corpus, so it is "
        "reporting a pass (or a skip) while measuring nothing. That is the "
        "defect: REQ-058 and REQ-035 are unscored and the build says otherwise."
    )
    assert marker.kwargs.get("strict") is True, (
        "the gate's xfail is not strict, so a corpus that starts working would "
        "keep reporting as a known failure forever"
    )
    assert marker.kwargs.get("run") is not False, (
        "the gate is marked xfail(run=False), which means it is not executed at "
        "all — the same silence as a skip, spelled differently"
    )
    reason = str(marker.kwargs.get("reason", ""))
    assert "R-01" in reason and "corpus" in reason.lower(), (
        "the reason a reader sees must name what is unmeasured and why: " + reason
    )


def test_the_gate_does_not_skip_itself() -> None:
    """A skip is how this defect was spelled for three phases.

    Structural rather than behavioural, because the gate itself only runs in the
    worker image with the OCR toolchain present — so the behavioural version of
    this check would be as invisible as the thing it is checking.
    """
    source = Path(gate.__file__).read_text()
    body = source[source.index("def test_golden_corpus_word_accuracy") :]
    assert "pytest.skip" not in body, (
        "the R-01 gate skips itself again. A skipped test in a run of a "
        "thousand passes is invisible; use the strict xfail, which is counted "
        "and reported as a failure that is known about."
    )


def test_any_staged_fixture_is_complete_and_hand_corrected() -> None:
    """The half-staged corpus, caught in the fast tier.

    `scripts/stage-corpus-fixture.py` pre-fills `expected.txt` with what OCR
    currently reads so that correcting a page takes minutes rather than an hour.
    An uncorrected file scores itself against its own output and returns 100%,
    which is worse than no measurement. `tests/ocr_scoring.py` refuses one at
    scoring time; this refuses it at collection time, before a half-hour OCR run
    has been spent to reach the same conclusion.
    """
    directories = [
        directory
        for directory in sorted(CORPUS_ROOT.iterdir())
        if directory.is_dir() and directory.name != "__pycache__"
    ]

    problems: list[str] = []
    for directory in directories:
        sources = list(directory.glob("source.*"))
        expected = directory / "expected.txt"
        if not sources and not expected.is_file():
            continue  # not a fixture directory at all
        if not sources:
            problems.append(f"{directory.name}: has expected.txt and no source.*")
            continue
        if not expected.is_file():
            problems.append(f"{directory.name}: has {sources[0].name} and no expected.txt")
            continue
        text = expected.read_text()
        if UNVERIFIED_MARKER in text:
            problems.append(
                f"{directory.name}: expected.txt still carries {UNVERIFIED_MARKER} — "
                "it is OCR's own output, so scoring against it measures nothing"
            )
        elif not text.strip():
            problems.append(f"{directory.name}: expected.txt is empty")

    assert not problems, (
        "the golden corpus has fixtures that cannot be scored:\n  "
        + "\n  ".join(problems)
    )


def test_the_corpus_state_is_reported_rather_than_assumed(record_property) -> None:
    """Print the figure's status, every run, wherever the output is read.

    Not an assertion about the number — there is no number. An assertion that
    the absence is stated out loud, so `make test` says what is unmeasured
    instead of leaving it to whoever remembers to read CLAUDE.md.
    """
    fixtures = gate.real_fixtures()
    record_property("r01_corpus_fixtures", len(fixtures))
    if fixtures:
        print(
            f"\nR-01: {len(fixtures)} corpus fixture(s) staged "
            f"({', '.join(directory.name for directory in fixtures)}). "
            "Run `make ocr-report` for the figure."
        )
    else:
        print(
            "\nR-01 UNMEASURED: tests/corpus/ has no real fixtures, so OCR word "
            "accuracy has never been scored. REQ-058 (auto-file precision) and "
            "REQ-035 (boundary F1) are unscored consequences of that, and the "
            "gate below reports as xfail rather than as a pass."
        )
    assert isinstance(fixtures, list)
