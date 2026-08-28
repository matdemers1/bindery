"""T-2.2 — boundary proposers (REQ-034).

Each proposer must fire on its own fixture **and stay silent on the others**.
Silence is the correct output for most seams in most files: the common case is a
scan that is one document, and a proposer that guesses eagerly would cut it up.
"""

from worker.segment.heuristics import (
    PageFeatures,
    propose_blank_page,
    propose_form_number,
    propose_layout_shift,
    propose_page_number_reset,
)
from worker.segment.heuristics.base import features_from_boxes
from worker.stages.segment import assemble, specs_from_boundaries

DD214_RULES = {
    "scope": "any_page",
    "all": [{"phrase": "certificate of release or discharge from active duty"}],
}


def page(
    number: int,
    *,
    text: str = "ordinary body text on a continuation sheet",
    words: int = 120,
    lines: int = 30,
    line_length: float = 48.0,
    left_margin: float = 90.0,
    footer: str = "",
) -> PageFeatures:
    return PageFeatures(
        number=number,
        text=text,
        word_count=words,
        line_count=lines,
        mean_line_length=line_length,
        left_margin=left_margin,
        top_ink=100.0,
        page_width=612.0,
        page_height=792.0,
        footer_text=footer,
    )


def blank(number: int) -> PageFeatures:
    return page(number, text="", words=0, lines=0, line_length=0.0, left_margin=0.0)


ORDINARY = [page(n) for n in range(1, 6)]


# --------------------------------------------------------------------------
# Blank page
# --------------------------------------------------------------------------


def test_a_blank_separator_starts_the_next_document() -> None:
    pages = [page(1), page(2), blank(3), page(4), page(5)]
    found = propose_blank_page(pages)
    # The boundary is after the blank, not at it: a trailing blank belongs to
    # the document it followed.
    assert [b.page for b in found] == [4]


def test_a_run_of_blanks_produces_one_boundary() -> None:
    pages = [page(1), blank(2), blank(3), blank(4), page(5)]
    assert [b.page for b in propose_blank_page(pages)] == [5]


def test_a_trailing_blank_separates_nothing() -> None:
    assert propose_blank_page([page(1), page(2), blank(3)]) == []


def test_near_blank_pages_still_count_as_blank() -> None:
    """Bleed-through and staple shadows produce a couple of spurious words."""
    pages = [page(1), page(2, text="3", words=2, lines=1), page(3)]
    assert [b.page for b in propose_blank_page(pages)] == [3]


def test_blank_page_is_silent_on_an_ordinary_file() -> None:
    assert propose_blank_page(ORDINARY) == []


# --------------------------------------------------------------------------
# Page-number reset
# --------------------------------------------------------------------------


def test_page_x_of_y_resetting_is_a_boundary() -> None:
    pages = [
        page(1, footer="Page 1 of 3"),
        page(2, footer="Page 2 of 3"),
        page(3, footer="Page 3 of 3"),
        page(4, footer="Page 1 of 2"),
        page(5, footer="Page 2 of 2"),
    ]
    assert [b.page for b in propose_page_number_reset(pages)] == [4]


def test_a_continuous_footer_is_not_a_boundary() -> None:
    pages = [page(n, footer=f"Page {n} of 5") for n in range(1, 6)]
    assert propose_page_number_reset(pages) == []


def test_a_bare_number_reset_votes_more_weakly() -> None:
    pages = [page(1, footer="1"), page(2, footer="2"), page(3, footer="1")]
    found = propose_page_number_reset(pages)
    assert [b.page for b in found] == [3]
    # On its own it must not clear the acceptance threshold.
    assert found[0].weight < 1.0


def test_page_number_reset_is_silent_without_footers() -> None:
    assert propose_page_number_reset(ORDINARY) == []


# --------------------------------------------------------------------------
# Form number
# --------------------------------------------------------------------------


def test_a_form_fingerprint_mid_bundle_is_a_boundary() -> None:
    pages = [
        page(1),
        page(2),
        page(3, text="CERTIFICATE OF RELEASE OR DISCHARGE FROM ACTIVE DUTY"),
        page(4),
    ]
    found = propose_form_number(pages, [("DD-214", DD214_RULES)])
    assert [b.page for b in found] == [3]


def test_a_multi_page_form_proposes_only_its_first_page() -> None:
    """Otherwise a four-page form cuts itself into four."""
    head = "CERTIFICATE OF RELEASE OR DISCHARGE FROM ACTIVE DUTY"
    pages = [page(1), page(2, text=head), page(3, text=head), page(4, text=head), page(5)]
    assert [b.page for b in propose_form_number(pages, [("DD-214", DD214_RULES)])] == [2]


def test_a_form_on_page_one_is_not_a_boundary() -> None:
    """The file already starts there."""
    pages = [page(1, text="CERTIFICATE OF RELEASE OR DISCHARGE FROM ACTIVE DUTY"), page(2)]
    assert propose_form_number(pages, [("DD-214", DD214_RULES)]) == []


def test_form_number_is_silent_with_an_empty_registry() -> None:
    assert propose_form_number(ORDINARY, []) == []
    assert propose_form_number(ORDINARY, None) == []


# --------------------------------------------------------------------------
# Layout shift
# --------------------------------------------------------------------------


def test_two_agreeing_layout_signals_produce_a_vote() -> None:
    pages = [
        page(1, lines=30, line_length=48.0, left_margin=90.0),
        page(2, lines=8, line_length=14.0, left_margin=260.0),
    ]
    found = propose_layout_shift(pages)
    assert [b.page for b in found] == [2]
    # The weakest proposer: never enough on its own.
    assert found[0].weight < 1.0


def test_one_layout_signal_alone_is_noise() -> None:
    pages = [
        page(1, lines=30, line_length=48.0, left_margin=90.0),
        page(2, lines=30, line_length=48.0, left_margin=260.0),
    ]
    assert propose_layout_shift(pages) == []


def test_layout_shift_ignores_seams_a_blank_page_owns() -> None:
    assert propose_layout_shift([page(1), blank(2)]) == []


def test_layout_shift_is_silent_on_a_uniform_file() -> None:
    assert propose_layout_shift(ORDINARY) == []


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def test_a_file_with_no_votes_becomes_one_document() -> None:
    """The common case, and the one it is most important not to get wrong."""
    specs = specs_from_boundaries([], 10)
    assert [(s.page_start, s.page_end) for s in specs] == [(1, 10)]


def test_boundaries_become_a_gapless_cover() -> None:
    specs = specs_from_boundaries([4, 8], 10)
    assert [(s.page_start, s.page_end) for s in specs] == [(1, 3), (4, 7), (8, 10)]


def test_weak_votes_alone_do_not_make_a_boundary() -> None:
    weak = propose_layout_shift(
        [page(1, lines=30, line_length=48.0, left_margin=90.0),
         page(2, lines=8, line_length=14.0, left_margin=260.0)]
    )
    assert assemble(weak, 2) == []


def test_weak_votes_combine_into_a_boundary() -> None:
    """Layout shift plus a bare footer reset is enough; either alone is not."""
    pages = [
        page(1, lines=30, line_length=48.0, left_margin=90.0, footer="4"),
        page(2, lines=8, line_length=14.0, left_margin=260.0, footer="1"),
    ]
    votes = propose_layout_shift(pages) + propose_page_number_reset(pages)
    accepted = assemble(votes, 2)
    assert [page_number for page_number, _ in accepted] == [2]


def test_page_one_is_never_a_boundary() -> None:
    from worker.segment.heuristics.base import Boundary

    assert assemble([Boundary(page=1, weight=99.0, reason="nonsense")], 5) == []


# --------------------------------------------------------------------------
# Feature extraction
# --------------------------------------------------------------------------


def test_features_are_derived_from_the_word_boxes() -> None:
    box_page = {
        "number": 3,
        "width": 612.0,
        "height": 792.0,
        "lines": [
            {"words": [{"x0": 90, "y0": 100, "x1": 140, "y1": 112, "t": "Hello"},
                       {"x0": 145, "y0": 100, "x1": 200, "y1": 112, "t": "world"}]},
            {"words": [{"x0": 90, "y0": 700, "x1": 160, "y1": 712, "t": "Page"},
                       {"x0": 165, "y0": 700, "x1": 180, "y1": 712, "t": "1"}]},
        ],
    }
    features = features_from_boxes(box_page, "Hello world\nPage 1")

    assert features.number == 3
    assert features.word_count == 4
    assert features.line_count == 2
    assert features.left_margin == 90.0
    # The footer band is the bottom 15% of the page: y >= 673.2.
    assert features.footer_text == "Page 1"
    assert not features.is_blank
