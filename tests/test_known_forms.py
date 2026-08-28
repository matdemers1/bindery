"""T-2.4 — the known-form registry (REQ-038, REQ-039).

> A registry match is a fact, not an opinion.

So the test that matters most is not "does the DD-214 match" but "does anything
*else* match the DD-214". **Precision is prioritised over recall**: a false
positive replaces a known unknown with a wrong known, which is worse.
"""

import pytest

from api.forms.matcher import best_match, compact, match, normalize
from api.forms.registry import load_seed_definitions

# Realistic page text for each seed form. Deliberately terse — these stand in
# for the head of a real document, not the whole thing.
FIXTURES: dict[str, list[str]] = {
    "DD-214": [
        "DEPARTMENT OF DEFENSE\n"
        "CERTIFICATE OF RELEASE OR DISCHARGE FROM ACTIVE DUTY\n"
        "1. NAME (Last, First, Middle)  DEMERS, MATTHEW\n"
        "12. RECORD OF SERVICE\n"
        "a. Date Entered AD This Period ..... 2009-06-15\n"
        "b. Separation Date This Period ..... 2014-08-11\n"
        "DD FORM 214, AUG 2009"
    ],
    "DD-215": [
        "CORRECTION TO DD FORM 214, CERTIFICATE OF RELEASE OR DISCHARGE FROM ACTIVE DUTY\n"
        "DD FORM 215, OCT 1979\n"
        "Item 12b is corrected to read 2014-08-11."
    ],
    "VA-RATING": [
        "DEPARTMENT OF VETERANS AFFAIRS\nRating Decision\n"
        "Evaluation of tinnitus is 10 percent disabling.\n"
        "Reasons for Decision"
    ],
    "VA-AWARD": [
        "DEPARTMENT OF VETERANS AFFAIRS\nWe have granted the following benefits\n"
        "Your monthly entitlement amount is shown below.\nEffective date: 2015-01-01"
    ],
    "W-2": [
        "Form W-2 Wage and Tax Statement 2024\n"
        "c Employer's name, address, and ZIP code\n"
        "1 Wages, tips, other compensation  2 Federal income tax withheld"
    ],
    "1099": [
        "Form 1099-INT  Interest Income  2024\n"
        "PAYER'S TIN  RECIPIENT'S TIN\n1 Interest income $412.09"
    ],
    "1098": [
        "Form 1098  Mortgage Interest Statement  2024\n"
        "1 Mortgage interest received from payer(s)/borrower(s)\n"
        "2 Outstanding mortgage principal"
    ],
    "DEED": [
        "WARRANTY DEED\n"
        "The grantor does hereby grant, bargain, sell and convey unto the grantee\n"
        "Recorded in the office of the Register of Deeds, Book and Page 4412/119"
    ],
    "TITLE": [
        "CERTIFICATE OF TITLE - MOTOR VEHICLE\n"
        "VEHICLE IDENTIFICATION NUMBER 1HGCV1F34LA012345\n"
        "ODOMETER READING 41,208"
    ],
    "MORTGAGE-NOTE": [
        "PROMISSORY NOTE\n"
        "In return for a loan that I have received, I promise to pay\n"
        "BORROWER'S PROMISE TO PAY  principal and interest"
    ],
    "BIRTH-CERT": [
        "CERTIFICATE OF LIVE BIRTH\nSTATE FILE NUMBER 118-2024-004412\n"
        "NAME OF CHILD  DATE OF BIRTH  PLACE OF BIRTH"
    ],
    "PASSPORT": [
        "UNITED STATES OF AMERICA PASSPORT\nPASSPORT NO 512004412\n"
        "P<USADEMERS<<MATTHEW<<<<<<<<<<<<<<<<<<<<<<<<"
    ],
    "MARRIAGE": [
        "CERTIFICATE OF MARRIAGE\nDATE OF MARRIAGE 2018-09-22\n"
        "were joined in marriage, solemnized by the officiant named below"
    ],
    "INS-DEC": [
        "AUTOMOBILE POLICY DECLARATIONS\nNAMED INSURED  POLICY NUMBER 44-1192-08\n"
        "POLICY PERIOD 2026-01-01 to 2026-07-01\nCOVERAGE LIMITS"
    ],
}

# Documents that must never match anything in the registry.
DECOYS: dict[str, list[str]] = {
    "continuation-sheet": [
        "CONSOLIDATED SERVICE RECORD\nContinuation sheet. Administrative orders.\nPage 4 of 12"
    ],
    "utility-bill": [
        "CITY WATER DEPARTMENT\nStatement for the quarter\nAccount balance due $84.12"
    ],
    "letter-mentioning-dd214": [
        "Dear Mr Demers,\nPlease enclose a copy of your DD-214 with your application.\n"
        "We cannot process the request without it."
    ],
    "marriage-application": [
        "APPLICATION FOR A MARRIAGE LICENSE\nCounty Clerk's Office\n"
        "Both applicants must appear in person."
    ],
    "policy-booklet": [
        "AUTOMOBILE POLICY BOOKLET\nTABLE OF CONTENTS\nDECLARATIONS ... 3\nCOVERAGE LIMITS ... 9"
    ],
}


@pytest.fixture(scope="module")
def registry() -> list[tuple[str, dict]]:
    return [
        (definition["code"], definition.get("match_rules") or {})
        for definition in load_seed_definitions()
    ]


def test_the_seed_set_covers_every_form_the_requirements_name(registry) -> None:
    """REQ-039."""
    required = {
        "DD-214", "DD-215", "VA-RATING", "VA-AWARD", "W-2", "1099", "1098",
        "DEED", "TITLE", "MORTGAGE-NOTE", "BIRTH-CERT", "PASSPORT", "MARRIAGE",
        "INS-DEC",
    }
    assert required <= {code for code, _ in registry}


@pytest.mark.parametrize("code", sorted(FIXTURES))
def test_each_form_matches_its_own_fixture(registry, code) -> None:
    """Recall: the registry has to actually recognise the thing."""
    found = best_match(registry, FIXTURES[code])
    assert found is not None, f"{code} did not match its own fixture"
    assert found.code == code


@pytest.mark.parametrize("code", sorted(FIXTURES))
def test_no_form_matches_another_form_s_fixture(registry, code) -> None:
    """**Precision.** The test that actually protects trust."""
    rules = dict(registry)[code]
    for other, pages in FIXTURES.items():
        if other == code:
            continue
        assert match(rules, pages) is None, (
            f"{code} rules falsely matched the {other} fixture — a false positive "
            "on a known form is worse than a miss"
        )


@pytest.mark.parametrize("name", sorted(DECOYS))
def test_nothing_matches_a_decoy(registry, name) -> None:
    assert best_match(registry, DECOYS[name]) is None, (
        f"the {name} decoy matched a known form"
    )


def test_a_letter_mentioning_a_dd214_is_not_a_dd214(registry) -> None:
    """The failure mode this design exists to prevent, stated on its own."""
    assert best_match(registry, DECOYS["letter-mentioning-dd214"]) is None


def test_a_dd215_is_not_filed_as_a_dd214(registry) -> None:
    """A correction quotes the original's full title; the `none` clause saves us."""
    found = best_match(registry, FIXTURES["DD-215"])
    assert found is not None and found.code == "DD-215"


def test_a_bundle_is_too_long_to_be_a_dd214(registry) -> None:
    """`max_pages` — a 100-page service record is not itself a discharge form."""
    bundle = FIXTURES["DD-214"] + ["continuation sheet"] * 40
    assert match(dict(registry)["DD-214"], bundle) is None


def test_an_ambiguous_match_yields_nothing(registry) -> None:
    """Two facts that disagree are not a fact. Returning neither is the safe answer."""
    ambiguous = [FIXTURES["W-2"][0] + "\n" + FIXTURES["1098"][0]]
    assert best_match(registry, ambiguous) is None


def test_ocr_spacing_variants_of_a_form_number_all_match(registry) -> None:
    """OCR renders the same footer as "DD 214", "DD-214" or "DD214"."""
    rules = dict(registry)["DD-214"]
    head = (
        "CERTIFICATE OF RELEASE OR DISCHARGE FROM ACTIVE DUTY\n"
        "Date Entered AD This Period 2009-06-15\n"
    )
    for variant in ("DD FORM 214", "DD-214", "DD 214", "DD214", "dd form 214"):
        assert match(rules, [head + variant]) is not None, variant


def test_normalisation_folds_punctuation_and_case() -> None:
    assert normalize("DD-214!") == "dd 214"
    assert compact("DD - 214") == "dd214"
