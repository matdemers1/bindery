"""T-1.12 — the golden corpus and the OCR accuracy report (REQ-018).

Two corpora, measured by the same metric and never mixed:

- **The synthetic corpus** (`tests/synthetic_corpus.py`, BND-T-005) — invented
  documents in the archive's real shapes, rendered and damaged at test time,
  with exact ground truth. It runs in CI on every push, and its figure is
  written to the integration job's summary. It is gated against a committed
  per-fixture baseline (`tests/synthetic_corpus_baseline.json`): a fixture may
  not gain errors. It does **not** clear R-01.
- **The real golden corpus** (`tests/corpus/<name>/`) — the maintainer's own
  records, never committed (BND-ADR-014). **This is the R-01 gate**: below 90%,
  the Phase 3 classification design is built on unreliable text. Where it is
  absent — every CI runner — the gate is *skipped*, with its reason in the
  integration job's summary, never reported as a pass.

    make ocr-report
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from tests.corpus.fixtures import CLEAN_SCAN, render_text_page
from tests.ocr_scoring import Score, corpus_accuracy, report, report_markdown, score
from tests.synthetic_corpus import FIXTURES, render

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        shutil.which("ocrmypdf") is None,
        reason="OCR toolchain not present; run this suite with `make ocr-report`",
    ),
]

CORPUS_ROOT = Path(__file__).parent / "corpus"
GATE = 0.90
SYNTHETIC_BASELINE = Path(__file__).parent / "synthetic_corpus_baseline.json"
# A fixture may drift this far before it counts as a regression: Tesseract on a
# different CPU or a patch release of Leptonica moves a word or two.
SYNTHETIC_TOLERANCE = 0.03

UNMEASURED = (
    "R-01 UNMEASURED: tests/corpus/ holds no real fixtures, so OCR accuracy on "
    "the real archive has not been scored and REQ-058 and REQ-035 stay unscored. "
    "The real corpus is never committed (BND-ADR-014); stage fixtures in a working "
    "copy (scripts/stage-corpus-fixture.py), hand-correct them, and run "
    "`make ocr-report`. The synthetic corpus figure in this run does not clear R-01."
)


def real_fixtures() -> list[Path]:
    """Directories holding a real document plus its ground truth."""
    if not CORPUS_ROOT.is_dir():
        return []
    return sorted(
        directory
        for directory in CORPUS_ROOT.iterdir()
        if directory.is_dir()
        and (directory / "expected.txt").is_file()
        and any(directory.glob("source.*"))
    )


async def _ocr_to_text(source: Path, workspace: Path) -> str:
    """Run the real normalize stage's OCR and return the extracted text."""
    from worker.ocr.word_boxes import extract_word_boxes, page_text
    from worker.stages.normalize import _run_ocr

    workspace.mkdir(parents=True, exist_ok=True)
    normalized = workspace / "normalized.pdf"
    sidecar = workspace / "ocr.txt"
    await _run_ocr(
        source, normalized, sidecar, image=source.suffix.lower() != ".pdf"
    )
    boxes = await extract_word_boxes(normalized)
    return "\n".join(page_text(page) for page in boxes["pages"])


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "ocr"


async def test_the_scorer_counts_errors_the_way_it_claims_to() -> None:
    """The metric itself, before it is trusted to gate a phase."""
    perfect = score("x", "one two three", "one two three")
    assert perfect.errors == 0 and perfect.accuracy == 1.0

    # Case and edge punctuation are not errors.
    forgiving = score("x", "Honda Accord", "honda accord.")
    assert forgiving.errors == 0

    # One substitution in four words is 75%.
    substitution = score("x", "one two three four", "one two thref four")
    assert substitution.errors == 1
    assert substitution.accuracy == pytest.approx(0.75)

    # A deletion and an insertion both count.
    assert score("x", "one two three", "one three").errors == 1
    assert score("x", "one two three", "one two two three").errors == 1

    # Garbage cannot produce a negative accuracy.
    assert score("x", "one two", "a b c d e f g h").accuracy == 0.0


async def test_ocr_reads_a_clean_synthetic_page(workspace) -> None:
    """Sanity floor: if this regresses, OCR is broken, not merely worse."""
    source = render_text_page(CLEAN_SCAN, workspace / "clean.png")
    text = await _ocr_to_text(source, workspace / "out")

    result = score("clean-synthetic", CLEAN_SCAN, text)
    print(report([result], r01=False))
    assert result.accuracy >= GATE, "OCR cannot read a clean rendered page"


async def test_deskew_and_clean_help_a_bad_scan(workspace) -> None:
    """REQ-013 — measured, not assumed.

    A skewed, speckled page is scored with the preprocessing options on. This is
    the quality floor the real bad-scan fixture will replace.
    """
    source = render_text_page(
        CLEAN_SCAN, workspace / "bad.png", rotation=1.6, noise=9000
    )
    text = await _ocr_to_text(source, workspace / "out")

    result = score("bad-scan-synthetic", CLEAN_SCAN, text)
    print(report([result], r01=False))
    # Deliberately below the corpus gate: this fixture is meant to be hard, and
    # the point is to notice the day it gets harder.
    assert result.accuracy >= 0.70, "preprocessing no longer rescues a bad scan"


async def test_synthetic_corpus_word_accuracy(workspace) -> None:
    """The measured figure CI can produce (BND-T-005, REQ-018).

    Every fixture in `tests/synthetic_corpus.py` is rendered, OCR'd by the real
    normalize stage, and scored. The report goes to stdout and, when CI sets
    `OCR_REPORT_DIR`, to a Markdown file the integration job puts in its summary.

    Gated against the committed baseline rather than a threshold: the damaged
    fixtures are meant to be hard, and a gate red on arrival is a gate somebody
    switches off. A fixture that gains errors fails; one that loses them passes
    and says so, so the baseline can be tightened.
    """
    baseline: dict[str, int] = json.loads(SYNTHETIC_BASELINE.read_text())["errors"]
    assert sorted(baseline) == sorted(f.name for f in FIXTURES), (
        "tests/synthetic_corpus_baseline.json does not cover exactly the fixtures in "
        "tests/synthetic_corpus.py — a new fixture needs a measured baseline, and a "
        "removed one must leave it"
    )

    scores: list[Score] = []
    for fixture in FIXTURES:
        source = render(fixture, workspace / fixture.name / "source")
        text = await _ocr_to_text(source, workspace / fixture.name / "ocr")
        scores.append(score(fixture.name, fixture.expected, text))

    print(report(scores, r01=False))
    if directory := os.environ.get("OCR_REPORT_DIR"):
        Path(directory, "ocr-report.md").write_text(
            report_markdown(scores, title="OCR word accuracy — synthetic corpus (not R-01)")
        )

    regressions, improvements = [], []
    for result in scores:
        slack = max(2, round(result.reference_words * SYNTHETIC_TOLERANCE))
        allowed = baseline[result.name] + slack
        if result.errors > allowed:
            regressions.append(
                f"{result.name}: {result.errors} errors, baseline {baseline[result.name]} "
                f"(at most {allowed} allowed)"
            )
        elif result.errors < baseline[result.name]:
            improvements.append(f"{result.name}: {baseline[result.name]} -> {result.errors}")
    if improvements:
        print(
            "\nFewer OCR errors than the baseline — tighten "
            "tests/synthetic_corpus_baseline.json:\n  " + "\n  ".join(improvements)
        )
    assert not regressions, "OCR got worse on the synthetic corpus:\n  " + "\n  ".join(
        regressions
    )


@pytest.mark.skipif(not real_fixtures(), reason=UNMEASURED)
async def test_golden_corpus_word_accuracy(workspace) -> None:
    """The report. **This is the R-01 gate.**

    Skipped — declared, with its reason, never passed — when no real fixtures
    are staged, which is every CI runner: the corpus is never committed. CI
    lists every skip and its reason in the integration job's summary
    (`scripts/ci_test_summary.py`), so the absence is read rather than buried
    in a count. With fixtures present it runs for real, and a corpus under 90%
    fails the build outright.
    """
    fixtures = real_fixtures()
    assert fixtures, UNMEASURED

    scores: list[Score] = []
    for directory in fixtures:
        source = next(iter(directory.glob("source.*")))
        expected = (directory / "expected.txt").read_text()
        text = await _ocr_to_text(source, workspace / directory.name)
        scores.append(score(directory.name, expected, text))

    print(report(scores))
    accuracy = corpus_accuracy(scores)
    assert accuracy >= GATE, (
        f"Corpus OCR word accuracy {accuracy * 100:.1f}% is below the {GATE * 100:.0f}% "
        "R-01 gate. Do not start Phase 3 on this text — improve preprocessing, "
        "escalate to PaddleOCR, or re-scan the sources first."
    )

