"""The R-01 gate must never report a pass while measuring nothing (CR-037).

`tests/test_ocr_accuracy.py::test_golden_corpus_word_accuracy` is the gate that
decides whether Phase 3's classification design is built on text good enough to
classify. Its corpus is the owner's real DD-214, VA medical records and
financial statements, which are never committed (BND-ADR-014) — so on every CI
runner it has nothing to measure.

It once called `pytest.skip` from inside its body, and the CI `integration` job
reported green having measured nothing; that is what let Phase 3 ship with
REQ-058 unscored. It was then a strict xfail, which was counted but still read
as one number inside a green run. Now (BND-T-005) it is a **declared skip**
whose reason is put, by name, on the integration job's summary page by
`scripts/ci_test_summary.py` — and a synthetic corpus beside it produces a
figure CI *can* measure, without pretending to be the real one.

So this file asserts, structurally and on every run:

1. the gate's absence is a declared skip with a reason that names R-01, never
   a skip from inside the body and never an xfail,
2. that skip goes away by itself the moment real fixtures are staged,
3. CI writes every skip and its reason to the job summary,
4. any fixture that *is* staged is complete and hand-corrected, so a half-staged
   corpus fails in seconds rather than after a full OCR run.
"""

import importlib.util
from pathlib import Path

from tests import test_ocr_accuracy as gate
from tests.ocr_scoring import UNVERIFIED_MARKER

REPO = Path(__file__).resolve().parent.parent
CORPUS_ROOT = Path(__file__).resolve().parent / "corpus"


def _markers(function, name: str) -> list:
    return [mark for mark in getattr(function, "pytestmark", []) if mark.name == name]


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


def test_an_empty_corpus_is_a_declared_skip_never_a_pass() -> None:
    """The whole finding, in one place.

    With no fixtures the gate is skipped by its marker, so the skip and its
    reason exist at collection time, where the junit report and the job summary
    see them. With fixtures, the marker's condition is false and the gate runs.
    """
    gate_function = gate.test_golden_corpus_word_accuracy
    assert not _markers(gate_function, "xfail"), (
        "the R-01 gate is an xfail again — it reads as one more number in a green "
        "run. With no corpus it is a declared skip whose reason CI puts on the "
        "summary page"
    )
    skips = _markers(gate_function, "skipif")
    assert len(skips) == 1, "the R-01 gate must carry exactly one skipif, on the corpus"
    condition = skips[0].args[0] if skips[0].args else skips[0].kwargs.get("condition")
    reason = str(skips[0].kwargs.get("reason", ""))

    if gate.real_fixtures():
        assert condition is False, (
            "real corpus fixtures are staged and the R-01 gate is still skipped, "
            "so an actual measurement is being thrown away"
        )
        return

    assert condition is True, (
        "there is no corpus and the R-01 gate is not skipped, so it runs and "
        "fails — or worse, passes — with nothing to measure"
    )
    assert "R-01" in reason and "corpus" in reason.lower() and "UNMEASURED" in reason, (
        "the reason a reader sees on the summary page must say what is unmeasured "
        "and why: " + reason
    )


def test_the_gate_does_not_skip_itself() -> None:
    """A skip from inside the body is how this defect was spelled for three phases.

    It is decided only after setup, carries whatever message the body chose, and
    looks the same as any other skip. The declared marker is the one place the
    absence is allowed to be stated.
    """
    source = Path(gate.__file__).read_text()
    body = source[source.index("async def test_golden_corpus_word_accuracy") :]
    assert "pytest.skip(" not in body, (
        "the R-01 gate skips itself from inside its body again; declare it on the "
        "skipif marker, where the junit report and the job summary see it"
    )


def test_ci_puts_every_skip_on_the_summary_page() -> None:
    """A declared skip is only honest if someone reads it.

    The integration job is where the gate is collected, so that job must write
    its junit results somewhere the runner can see them and turn them into the
    summary even when the run fails.
    """
    workflow = (REPO / ".github" / "workflows" / "build.yml").read_text()
    job = workflow[workflow.index("\n  integration:") : workflow.index("\n  e2e:")]
    assert "--junitxml=/out/integration.xml" in job, "the integration job writes no junit results"
    assert "OCR_REPORT_DIR=/out" in job, "the synthetic OCR figure has nowhere to go"
    step = job[job.index("scripts/ci_test_summary.py") - 600 :]
    assert "if: always()" in step, "the summary must be written when the run fails, too"
    assert '>> "$GITHUB_STEP_SUMMARY"' in step, "the summary step does not write the summary"


def _summary_module():
    spec = importlib.util.spec_from_file_location(
        "ci_test_summary", REPO / "scripts" / "ci_test_summary.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_summary_names_each_skip_and_its_reason(tmp_path) -> None:
    junit = tmp_path / "results.xml"
    junit.write_text(
        '<testsuites><testsuite tests="3" failures="0" errors="0" skipped="2">'
        '<testcase classname="tests.test_ocr_accuracy" name="test_synthetic"/>'
        '<testcase classname="tests.test_ocr_accuracy" name="test_golden_corpus_word_accuracy">'
        '<skipped type="pytest.skip" message="R-01 UNMEASURED: no corpus">x</skipped></testcase>'
        '<testcase classname="tests.test_x" name="test_known">'
        '<skipped type="pytest.xfail" message="a known | failure"/></testcase>'
        "</testsuite></testsuites>"
    )
    summary = _summary_module().summarize(junit)

    assert "3 run, 0 failed, 0 errors, 2 skipped or xfailed" in summary
    gate_row = "`tests.test_ocr_accuracy::test_golden_corpus_word_accuracy`"
    assert f"| skipped | {gate_row} | R-01 UNMEASURED: no corpus |" in summary
    assert "| xfail | `tests.test_x::test_known` | a known \\| failure |" in summary


def test_a_run_that_wrote_no_results_says_so(tmp_path) -> None:
    assert "No test results" in _summary_module().summarize(tmp_path / "missing.xml")


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
            "gate is skipped with that reason on the CI summary page. The "
            "synthetic corpus figure is measured, but it is not R-01."
        )
    assert isinstance(fixtures, list)
