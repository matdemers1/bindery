"""ADR-003 — Claude is reached through one adapter, not three (BND-FR-002).

The adapter is not housekeeping. Sending the OCR text of medical, identity and
financial documents off the network is a deliberate, accepted trade, and ADR-003
names this interface as the reversal path: a local or hybrid backend is meant to
be a configuration change rather than a rewrite. A swap point only swaps if
there is one of it, so the count is the requirement.

There were three call sites. `ClaudeProvider`, the sanctioned one;
`ClaudeAnswerer`, which built its own client and could not implement
`AIProvider` because `api/` may not import `worker/`; and a bare
`anthropic.AsyncAnthropic` inline in the `POST /api/settings/test-ai` handler —
so the one screen whose entire purpose is "check that my key works" exercised a
client the pipeline never runs.

This is a structural test, on the same reasoning as
`tests/test_no_destructive_paths.py`: the property is "there is exactly one",
which no behavioural test can observe. If it fails, route the new call site
through `api/ai_client.py` rather than adding a name to the exemption.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEARCHED = ("api", "worker")

# The one module allowed to construct a client. Anything else goes through it.
CONSTRUCTION_POINT = Path("api/ai_client.py")

CONSTRUCTS_CLIENT = re.compile(r"\bAsyncAnthropic\s*\(|\bAnthropic\s*\(")
TAXONOMY = re.compile(
    r"class (AIProviderError|ProviderRefusedError|ProviderUnavailableError)\b"
)


def _python_files() -> list[Path]:
    return sorted(
        path
        for package in SEARCHED
        for path in (ROOT / package).rglob("*.py")
    )


def test_only_one_module_constructs_an_anthropic_client() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in _python_files()
        if path.relative_to(ROOT) != CONSTRUCTION_POINT
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if CONSTRUCTS_CLIENT.search(line)
    ]

    assert not offenders, (
        "these construct their own Anthropic client instead of going through "
        f"{CONSTRUCTION_POINT}:\n  " + "\n  ".join(offenders)
    )


def test_the_construction_point_actually_constructs_one() -> None:
    """The guard above passes trivially if the one permitted site stops existing.

    Same failure mode as the screenshot manifest and the route-coverage sweep:
    a check that examines nothing reports nothing wrong.
    """
    source = (ROOT / CONSTRUCTION_POINT).read_text()
    assert CONSTRUCTS_CLIENT.search(source), (
        f"{CONSTRUCTION_POINT} no longer builds a client, so the guard above is "
        "asserting that nothing happens anywhere"
    )


def test_the_error_taxonomy_is_defined_once() -> None:
    """`worker/ai/provider.py` re-exports it; it must not redefine it.

    The taxonomy is the half of the adapter the retry policy reads — whether a
    failure is retried forever, retried five times, or never — and two
    definitions of it is two retry policies. `confirm_boundaries` had its own:
    a 429 was "upstream error 429" rather than "rate limited", and an exhausted
    credit balance dead-lettered every queued document in about four minutes.
    """
    defined = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in _python_files()
        if path.relative_to(ROOT) != CONSTRUCTION_POINT
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if TAXONOMY.match(line)
    ]

    assert not defined, (
        "the provider error taxonomy is defined outside "
        f"{CONSTRUCTION_POINT}:\n  " + "\n  ".join(defined)
    )
