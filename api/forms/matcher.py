"""Deterministic known-form matching.

> A registry match is a fact, not an opinion.

That framing is the whole design constraint. Nothing here scores, ranks, or
guesses: a form either matches its rules or it does not. **Precision is
prioritised over recall** (REQ-038) — a false "this is your DD-214" is worse
than no answer at all, because it replaces a known unknown with a wrong known.

Rule shape (see `worker/forms/seed/*.yaml`):

    scope: first_page | any_page | all_pages
    max_pages: 4                # a 90-page bundle is not a DD-214
    all:  [ ... ]               # every one of these must match
    any:  [ ... ]               # at least one of these must match
    none: [ ... ]               # none of these may match

A clause is `{phrase: "..."}` or `{regex: "..."}`. Phrases are matched against
normalised text; regexes against the same, so a seed author never has to guess
how OCR spaced a form number.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

# OCR renders "DD-214", "DD 214" and "DD214" for the same footer, and swaps in
# any of several dash characters. Normalisation folds them together so a rule
# only has to be written once.
_PUNCTUATION = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", folded)).strip()


def compact(text: str) -> str:
    """Normalised text with spaces removed, so "dd 214" also matches "dd214"."""
    return normalize(text).replace(" ", "")


@dataclass(frozen=True)
class FormMatch:
    code: str
    # Which pages of the candidate satisfied the rules — useful in the UI, and
    # the thing a human checks when they doubt a match.
    matched_pages: list[int]
    reason: str


def _clause_matches(clause: dict[str, Any], normalized: str, compacted: str) -> bool:
    if "phrase" in clause:
        phrase = clause["phrase"]
        return normalize(phrase) in normalized or compact(phrase) in compacted
    if "regex" in clause:
        return re.search(clause["regex"], normalized, re.IGNORECASE) is not None
    raise ValueError(f"unrecognised match clause: {clause!r}")


def _page_matches(rules: dict[str, Any], text: str) -> bool:
    normalized = normalize(text)
    compacted = compact(text)

    required = rules.get("all") or []
    if not all(_clause_matches(clause, normalized, compacted) for clause in required):
        return False

    alternatives = rules.get("any") or []
    if alternatives and not any(
        _clause_matches(clause, normalized, compacted) for clause in alternatives
    ):
        return False

    return True


def _disqualified(rules: dict[str, Any], pages: list[str]) -> bool:
    """A `none` clause anywhere in the candidate kills the match outright.

    Scoped to the whole candidate rather than the matching page, because the
    words that mean "this is a worksheet, not the real thing" are rarely on the
    same page as the form number.
    """
    forbidden = rules.get("none") or []
    if not forbidden:
        return False
    for text in pages:
        normalized = normalize(text)
        compacted = compact(text)
        if any(_clause_matches(clause, normalized, compacted) for clause in forbidden):
            return True
    return False


def match(rules: dict[str, Any], pages: list[str]) -> list[int] | None:
    """Return the 1-based indices of matching pages, or None.

    `pages` is the candidate document's page text in order.
    """
    if not rules or not pages:
        return None

    max_pages = rules.get("max_pages")
    if max_pages is not None and len(pages) > max_pages:
        return None

    if _disqualified(rules, pages):
        return None

    scope = rules.get("scope", "any_page")
    if scope == "first_page":
        candidates = [1] if _page_matches(rules, pages[0]) else []
    elif scope == "all_pages":
        matched = [i for i, text in enumerate(pages, start=1) if _page_matches(rules, text)]
        candidates = matched if len(matched) == len(pages) else []
    else:
        candidates = [i for i, text in enumerate(pages, start=1) if _page_matches(rules, text)]

    return candidates or None


def best_match(
    registry: list[tuple[str, dict[str, Any]]], pages: list[str]
) -> FormMatch | None:
    """Match a candidate against the whole registry.

    Two forms matching the same document means the rules are ambiguous, and an
    ambiguous fact is not a fact — so nothing is returned rather than a coin
    toss. The registry is small and curated; this is a signal to fix a rule.
    """
    hits = [
        FormMatch(code=code, matched_pages=matched, reason=f"matched {code} rules")
        for code, rules in registry
        if (matched := match(rules, pages)) is not None
    ]
    if len(hits) != 1:
        return None
    return hits[0]
