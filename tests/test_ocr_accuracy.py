"""T-1.12 — the golden corpus and the OCR accuracy report (REQ-018).

**This is the R-01 gate.** If corpus word accuracy on the real military and
house bundles comes in below 90%, the Phase 3 classification design is built on
unreliable text and gets re-planned before any of it is written.

Real fixtures live in `tests/corpus/<name>/` as `source.<ext>` + `expected.txt`
and are genuine personal records, so they exist only in this private repo. When
none are present the suite still runs — against synthetic pages, which verify
the *harness* and nothing about the real archive. The distinction is printed
loudly, because a green synthetic run is not the gate.

    make ocr-report
"""

import shutil
from pathlib import Path

import pytest

from tests.corpus.fixtures import CLEAN_SCAN, render_text_page
from tests.ocr_scoring import Score, corpus_accuracy, report, score

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        shutil.which("ocrmypdf") is None,
        reason="OCR toolchain not present; run this suite with `make ocr-report`",
    ),
]

CORPUS_ROOT = Path(__file__).parent / "corpus"
GATE = 0.90


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
    print(report([result]))
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
    print(report([result]))
    # Deliberately below the corpus gate: this fixture is meant to be hard, and
    # the point is to notice the day it gets harder.
    assert result.accuracy >= 0.70, "preprocessing no longer rescues a bad scan"


async def test_golden_corpus_word_accuracy(workspace) -> None:
    """The report. **This is the R-01 gate.**"""
    fixtures = real_fixtures()
    if not fixtures:
        pytest.skip(
            "No real corpus fixtures found in tests/corpus/. The R-01 gate is NOT "
            "cleared by a synthetic run — add tests/corpus/<name>/source.* plus "
            "expected.txt for the real military and house bundles and re-run."
        )

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
