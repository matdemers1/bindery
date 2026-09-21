"""Resolving a unification proposal back to records (BND-ADR-003, BND-FR-003).

The model is handed a list and answers with groups. Turning that answer back into rows is the
step where a mistake is permanent: these proposals merge correspondents, document types and tags,
so resolving to the wrong row — or quietly to fewer rows than exist — moves documents into a
folder nobody chose.

The reply used to be matched against a dict keyed on the case-folded name. Two rows really can
both be called "VERIZON", and that dict kept one of them.
"""

import json
import uuid

from api.unify import _parse


def _entry(name: str, documents: int = 1, entry_id: str | None = None) -> dict:
    return {"id": entry_id or str(uuid.uuid4()), "name": name, "documents": documents}


def _answer(groups: list[dict]) -> str:
    return json.dumps({"groups": groups})


def test_members_resolve_by_id() -> None:
    a, b = _entry("Verizon", 10), _entry("Verizon Wireless", 4)
    names = [a, b]

    proposal = _parse(
        _answer([{"canonical": a["id"], "members": [a["id"], b["id"]], "reason": "same carrier"}]),
        names, model="test",
    )

    assert len(proposal.groups) == 1
    group = proposal.groups[0]
    assert {m["id"] for m in group.members} == {a["id"], b["id"]}
    assert group.canonical_id == uuid.UUID(a["id"])


def test_two_rows_with_the_same_name_are_both_kept() -> None:
    # The bug. Both rows exist, differing only in case; a dict keyed on the folded name held one.
    loud, quiet = _entry("VERIZON", 3), _entry("Verizon", 9)
    names = [loud, quiet]

    proposal = _parse(
        _answer([{
            "canonical": quiet["id"],
            "members": [loud["id"], quiet["id"]],
            "reason": "one company",
        }]),
        names, model="test",
    )

    assert len(proposal.groups) == 1
    assert {m["id"] for m in proposal.groups[0].members} == {loud["id"], quiet["id"]}, (
        "both rows must survive: merging one of two duplicates leaves the other behind"
    )


def test_a_name_reply_still_resolves_and_keeps_every_row_of_that_name() -> None:
    # A model that ignores the instruction and answers with names must not cost a row either.
    loud, quiet = _entry("VERIZON", 3), _entry("Verizon", 9)
    other = _entry("Comcast", 2)

    proposal = _parse(
        _answer([{
            "canonical": "Verizon",
            "members": ["Verizon", "VERIZON"],
            "reason": "one company",
        }]),
        [loud, quiet, other], model="test",
    )

    assert len(proposal.groups) == 1
    assert {m["id"] for m in proposal.groups[0].members} == {loud["id"], quiet["id"]}
    assert other["id"] not in {m["id"] for m in proposal.groups[0].members}


def test_an_unknown_reference_is_dropped_not_invented() -> None:
    a, b = _entry("Verizon", 10), _entry("Comcast", 4)

    proposal = _parse(
        _answer([{"canonical": a["id"], "members": [a["id"], str(uuid.uuid4())], "reason": "?"}]),
        [a, b], model="test",
    )

    # One real member is not a group, and a hallucinated id resolves to nothing rather than to
    # whatever sorts nearest.
    assert proposal.groups == []


def test_the_survivor_is_always_one_of_the_members() -> None:
    a, b = _entry("Verizon", 10), _entry("Verizon Wireless", 4)
    outsider = _entry("Comcast", 99)

    proposal = _parse(
        _answer([{"canonical": outsider["id"], "members": [a["id"], b["id"]], "reason": "same"}]),
        [a, b, outsider], model="test",
    )

    # Naming a row outside the group as canonical would merge these documents into Comcast.
    assert len(proposal.groups) == 1
    assert proposal.groups[0].canonical_id in {uuid.UUID(a["id"]), uuid.UUID(b["id"])}
    assert proposal.groups[0].canonical_id == uuid.UUID(a["id"]), "the most-used member survives"


def test_a_group_of_one_merges_nothing() -> None:
    a = _entry("Verizon", 10)
    proposal = _parse(_answer([{"canonical": a["id"], "members": [a["id"]], "reason": "alone"}]),
                      [a], model="test")
    assert proposal.groups == []
