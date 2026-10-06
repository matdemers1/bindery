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
from collections import Counter
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


def unordered_errors(reference: list[str], hypothesis: list[str]) -> int:
    """Edit distance with word order forgiven: the recognition errors alone.

    Each unmatched reference word pairs with an unmatched hypothesis word as one
    substitution, and the remainder are insertions or deletions, so this is the
    larger of the two multiset differences. It can never exceed
    `edit_distance` — the gap between the two is what reading order cost.

    It exists because the R-01 figure is order-sensitive, on purpose: page text
    is stored in the order the text layer gives it, and that order is what a
    snippet or a classifier reads. But a form whose boxes come out shuffled and a
    page whose words are misread both lower that one number, and they have
    different remedies. This separates them.
    """
    ref, hyp = Counter(reference), Counter(hypothesis)
    return max(sum((ref - hyp).values()), sum((hyp - ref).values()))


@dataclass(frozen=True)
class Score:
    name: str
    reference_words: int
    errors: int
    unordered: int = 0

    @property
    def accuracy(self) -> float:
        if self.reference_words == 0:
            return 0.0
        # Insertions can push errors above the reference length; a negative
        # accuracy is not meaningful, so clamp at zero.
        return max(0.0, 1.0 - self.errors / self.reference_words)

    @property
    def recognition(self) -> float:
        """Accuracy with reading order forgiven — see `unordered_errors`."""
        if self.reference_words == 0:
            return 0.0
        return max(0.0, 1.0 - self.unordered / self.reference_words)


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
        name=name,
        reference_words=len(reference),
        errors=edit_distance(reference, hypothesis),
        unordered=unordered_errors(reference, hypothesis),
    )


def report(scores: list[Score], *, r01: bool = True) -> str:
    """A table, plus the corpus-wide figure.

    `r01=False` is the synthetic corpus: the same metric, but a figure that does
    not clear R-01, so the last line must not say PASS or FAIL against it.
    """
    total_words = sum(s.reference_words for s in scores)
    total_errors = sum(s.errors for s in scores)
    overall = corpus_accuracy(scores)
    recognition = corpus_recognition(scores)

    width = max((len(s.name) for s in scores), default=8)
    rule = f"{'-' * width}  {'-' * 7}  {'-' * 7}  {'-' * 9}  {'-' * 11}"
    lines = [
        "",
        f"{'fixture'.ljust(width)}  {'words':>7}  {'errors':>7}  "
        f"{'accuracy':>9}  {'recognition':>11}",
        rule,
    ]
    for s in sorted(scores, key=lambda s: s.accuracy):
        lines.append(
            f"{s.name.ljust(width)}  {s.reference_words:>7}  {s.errors:>7}  "
            f"{s.accuracy * 100:>8.1f}%  {s.recognition * 100:>10.1f}%"
        )
    lines += [
        rule,
        f"{'CORPUS'.ljust(width)}  {total_words:>7}  {total_errors:>7}  "
        f"{overall * 100:>8.1f}%  {recognition * 100:>10.1f}%",
        "",
        "accuracy: word edit distance in stored order (the R-01 metric).",
        "recognition: the same with reading order forgiven.",
        "",
        f"R-01 gate: >= 90.0%  ->  {'PASS' if overall >= 0.90 else 'FAIL'}"
        if r01
        else "Synthetic corpus: a measured baseline, not the R-01 gate.",
        "",
    ]
    return "\n".join(lines)


def report_markdown(scores: list[Score], *, title: str) -> str:
    """The same report as a Markdown table, for a CI job summary."""
    lines = [
        f"### {title}",
        "",
        "| fixture | words | errors | accuracy | recognition |",
        "|---|---:|---:|---:|---:|",
    ]
    for s in sorted(scores, key=lambda s: s.accuracy):
        lines.append(
            f"| {s.name} | {s.reference_words} | {s.errors} | "
            f"{s.accuracy * 100:.1f}% | {s.recognition * 100:.1f}% |"
        )
    lines.append(
        f"| **corpus** | {sum(s.reference_words for s in scores)} | "
        f"{sum(s.errors for s in scores)} | **{corpus_accuracy(scores) * 100:.1f}%** | "
        f"{corpus_recognition(scores) * 100:.1f}% |"
    )
    lines += [
        "",
        "*accuracy* is word edit distance in stored order (the R-01 metric); "
        "*recognition* forgives reading order.",
        "",
    ]
    return "\n".join(lines)


def corpus_accuracy(scores: list[Score]) -> float:
    total_words = sum(s.reference_words for s in scores)
    if not total_words:
        return 0.0
    return max(0.0, 1.0 - sum(s.errors for s in scores) / total_words)


def corpus_recognition(scores: list[Score]) -> float:
    total_words = sum(s.reference_words for s in scores)
    if not total_words:
        return 0.0
    return max(0.0, 1.0 - sum(s.unordered for s in scores) / total_words)
