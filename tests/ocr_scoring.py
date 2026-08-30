"""Word-accuracy scoring for OCR output (REQ-018).

`accuracy = 1 - (substitutions + deletions + insertions) / reference_words`,
i.e. word-level edit distance against ground truth. Reported as a percentage so
it can be compared directly against the **90% Phase 1 gate** in R-01.

Normalization before comparison: case-folded, punctuation stripped from word
edges, whitespace collapsed. OCR that reads `Honda.` for `Honda` is right about
the thing that matters, and counting that as an error would make the number
pessimistic in a way that hides real regressions.
"""

import re
import unicodedata
from dataclasses import dataclass

_EDGE_PUNCTUATION = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)


def normalize_words(text: str) -> list[str]:
    words: list[str] = []
    for raw in text.split():
        folded = unicodedata.normalize("NFKC", raw).casefold()
        stripped = _EDGE_PUNCTUATION.sub("", folded)
        if stripped:
            words.append(stripped)
    return words


def edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    """Levenshtein over word sequences, O(len(hypothesis)) memory."""
    if not reference:
        return len(hypothesis)
    previous = list(range(len(hypothesis) + 1))
    for i, ref_word in enumerate(reference, start=1):
        current = [i]
        for j, hyp_word in enumerate(hypothesis, start=1):
            current.append(
                min(
                    previous[j] + 1,                                  # deletion
                    current[j - 1] + 1,                               # insertion
                    previous[j - 1] + (ref_word != hyp_word),         # substitution
                )
            )
        previous = current
    return previous[-1]


@dataclass(frozen=True)
class Score:
    name: str
    reference_words: int
    errors: int

    @property
    def accuracy(self) -> float:
        if self.reference_words == 0:
            return 0.0
        # Insertions can push errors above the reference length; a negative
        # accuracy is not meaningful, so clamp at zero.
        return max(0.0, 1.0 - self.errors / self.reference_words)


UNVERIFIED_MARKER = "# UNVERIFIED"


class UnverifiedFixture(Exception):
    """`expected.txt` still holds the OCR output it was staged from.

    Scoring OCR against its own output returns 100% and measures nothing — a
    number that looks like success and is the absence of a measurement. The
    marker is removed by the person who checked the text against the page.
    """


def score(name: str, expected: str, actual: str) -> Score:
    if expected.lstrip().startswith(UNVERIFIED_MARKER):
        raise UnverifiedFixture(
            f"{name}: expected.txt is still the staged OCR output. Correct it "
            "against the page and delete the first line."
        )

    reference = normalize_words(expected)
    hypothesis = normalize_words(actual)
    return Score(
        name=name, reference_words=len(reference), errors=edit_distance(reference, hypothesis)
    )


def report(scores: list[Score]) -> str:
    """A table, plus the corpus-wide figure that is the actual gate."""
    total_words = sum(s.reference_words for s in scores)
    total_errors = sum(s.errors for s in scores)
    overall = max(0.0, 1.0 - total_errors / total_words) if total_words else 0.0

    width = max((len(s.name) for s in scores), default=8)
    lines = [
        "",
        f"{'fixture'.ljust(width)}  {'words':>7}  {'errors':>7}  {'accuracy':>9}",
        f"{'-' * width}  {'-' * 7}  {'-' * 7}  {'-' * 9}",
    ]
    for s in sorted(scores, key=lambda s: s.accuracy):
        lines.append(
            f"{s.name.ljust(width)}  {s.reference_words:>7}  {s.errors:>7}  "
            f"{s.accuracy * 100:>8.1f}%"
        )
    lines += [
        f"{'-' * width}  {'-' * 7}  {'-' * 7}  {'-' * 9}",
        f"{'CORPUS'.ljust(width)}  {total_words:>7}  {total_errors:>7}  {overall * 100:>8.1f}%",
        "",
        f"R-01 gate: >= 90.0%  ->  {'PASS' if overall >= 0.90 else 'FAIL'}",
        "",
    ]
    return "\n".join(lines)


def corpus_accuracy(scores: list[Score]) -> float:
    total_words = sum(s.reference_words for s in scores)
    if not total_words:
        return 0.0
    return max(0.0, 1.0 - sum(s.errors for s in scores) / total_words)
