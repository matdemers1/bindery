"""A known form's fingerprint appearing mid-bundle starts a new document.

This is the proposer that finds the DD-214 inside a 100-page service record: the
registry already knows what the first page of one looks like, so the same rules
that identify a document can also locate where it begins.

Only the *first* page of a run votes. A four-page form matching on all four
would otherwise propose three spurious boundaries inside itself.
"""

from typing import Any

from api.forms.matcher import match
from worker.segment.heuristics.base import Boundary, PageFeatures

WEIGHT = 1.5


def propose_form_number(
    pages: list[PageFeatures], registry: list[tuple[str, dict[str, Any]]] | None = None
) -> list[Boundary]:
    if not registry:
        return []

    boundaries: list[Boundary] = []
    for code, rules in registry:
        # Match each page on its own, so `max_pages` does not disqualify the
        # form merely for sitting inside a large bundle.
        page_rules = {key: value for key, value in rules.items() if key != "max_pages"}
        hit_pages = [
            page.number for page in pages if match({**page_rules, "scope": "any_page"}, [page.text])
        ]
        for index, number in enumerate(hit_pages):
            is_run_continuation = index > 0 and hit_pages[index - 1] == number - 1
            if is_run_continuation or number == pages[0].number:
                continue
            boundaries.append(
                Boundary(
                    page=number,
                    weight=WEIGHT,
                    reason=f"page {number} matches the {code} fingerprint",
                )
            )
    return boundaries
