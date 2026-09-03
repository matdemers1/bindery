"""The two halves of the wire contract still agree (CR-059).

`api/schemas.py` declares 123 Pydantic response models. `web/src/api.ts` declares
96 TypeScript interfaces mirroring them, written by hand. They agree today —
every field the client reads exists on the server — and until now nothing in five
CI gates was checking that. `tsc --noEmit` proves the client is internally
consistent, not that it matches the server, so a renamed or narrowed response
field passes lint, unit, integration and e2e and arrives in the browser as
`undefined` on whichever screen the e2e specs happen not to assert on. On a UI
whose whole job is to make an automated decision inspectable, a blank field on
the Why panel is indistinguishable from an AI that had nothing to say.

The check itself is `scripts/check_api_contract.py`, so CI's **lint** gate can run
it without a database — the cheapest gate, which is the sequencing the workflow
already argues for. This runs the same code in the unit suite, and proves it can
fail: a contract check that has only ever been run against a repository that
already agrees is a check nobody has watched work.
"""

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_api_contract.py"


def load():
    spec = importlib.util.spec_from_file_location("check_api_contract", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return load()


def test_the_client_reads_no_field_the_server_does_not_send(checker):
    problems = checker.check()
    assert problems == [], "\n".join(["the API contract has drifted:", *problems])


def test_it_pairs_most_of_the_client_with_the_server(checker):
    """A check that quietly stopped matching anything would pass forever. The
    number is asserted from below rather than exactly, so adding models is free
    and gutting the pairing is not."""
    server, client = checker.server_models(), checker.client_interfaces()
    pairs, unpaired = checker.pair_up(server, client)
    assert len(server) > 100, "the schema parser found almost no models"
    assert len(client) > 80, "the client parser found almost no interfaces"
    assert len(pairs) >= 80, (
        f"only {len(pairs)} of {len(client)} client types pair with a model. The "
        "pairing rules have stopped working, and a check that examines nothing "
        "reports nothing wrong"
    )
    assert set(unpaired) <= set(checker.UNPAIRED)


def test_a_field_the_server_stopped_sending_is_caught(checker):
    """The failure this exists for: someone renames `document_date` on the model
    and the interface keeps the old name."""
    problems = checker.check(
        server={"DocumentOut": {"id", "title"}},
        client={"Document": {"id", "title", "document_date"}},
    )
    assert any("document_date" in problem for problem in problems), problems


def test_an_interface_with_no_model_and_no_reason_is_caught(checker):
    """Otherwise the hole widens silently, which is the same defect one level
    up: coverage that quietly shrinks while the check stays green."""
    problems = checker.check(server={}, client={"SomethingNew": {"id"}})
    assert any("SomethingNew" in problem for problem in problems), problems


def test_an_excuse_for_a_type_that_no_longer_exists_is_caught(checker):
    """`UNPAIRED` and `ALIASES` are hand-maintained lists, and a hand-maintained
    list nobody prunes becomes a list nobody trusts."""
    problems = checker.check(server={}, client={})
    assert any("UNPAIRED still excuses" in problem for problem in problems), problems
    assert any("ALIASES still maps" in problem for problem in problems), problems


def test_the_client_is_allowed_to_read_less_than_the_server_sends(checker):
    """Deliberately one-directional. Most screens read a subset, and failing on
    that would make the check noise within a week."""
    problems = checker.check(
        server={"DocumentOut": {"id", "title", "page_start"}},
        client={"Document": {"id"}},
    )
    assert not [problem for problem in problems if "`Document`" in problem], problems
