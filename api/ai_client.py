"""The one place Bindery builds a client for the Anthropic API (ADR-003, BND-FR-002).

ADR-003 puts Claude behind one adapter, and the reason is not tidiness: sending
the OCR text of medical, identity and financial documents off the network is a
deliberate, accepted trade, and the adapter is what keeps it reversible. A swap
point only swaps if there is one of it.

There were three. `ClaudeProvider` in `worker/ai/claude.py` — the sanctioned
one. `ClaudeAnswerer` in `api/ai_ask.py`, which constructed its own client and
could not be an `AIProvider` because `api/` may not import `worker/`. And a bare
`anthropic.AsyncAnthropic` inline in the `POST /api/settings/test-ai` handler,
which is how "check my key" ended up exercising a code path the pipeline does
not use — a test that passes against a client nothing else runs is a test of the
wrong thing.

This module lives in `api/` because that is the only package both images carry
(see the layering rule in CLAUDE.md), so it can be the single construction point
for a request the operator is waiting on *and* for a pipeline stage.

What it owns:

- **How a client is built**, and the fact that no key means no client rather
  than a client that fails at the first call.
- **Which model and key are in force**, read from the settings a human set in
  preference to the environment — so adding a key in the UI takes effect on the
  next document rather than on the next restart. Four call sites each carried
  their own copy of that lookup and their own literal default model.
- **What an exception from the API means.** `translate` is the taxonomy the
  worker's retry policy reads: unreachable and rate-limited are retried
  indefinitely, a 4xx is not, and an exhausted credit balance is retryable
  despite arriving as a 400, because topping the account up is exactly what
  makes the retry work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("bindery.ai")

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_PROMPT_VERSION = "v1"


class AIProviderError(RuntimeError):
    """The provider could not produce a valid result.

    Raised loudly and retried. A malformed response is **never** silently
    accepted or partially applied (REQ-045): half a classification looks like a
    confident answer, and a confidently wrong answer is the kill criterion.
    """


class ProviderRefusedError(AIProviderError):
    """A safety classifier declined the request.

    Permanent for this content and this model: the same document sent again
    gets the same answer, so retrying is five attempts to reach one conclusion.

    It is worth being specific about how this presented, because it cost two
    wrong diagnoses. The API returns HTTP 200 with `stop_reason="refusal"` and
    no content — which looked exactly like a malformed response, was reported as
    "structured output was empty", and sent the investigation to `max_tokens`
    twice. The refusal is now read before the content.
    """

    def __init__(self, message: str, *, category: str | None = None) -> None:
        self.category = category
        super().__init__(message)


class ProviderUnavailableError(AIProviderError):
    """The provider is unreachable or unconfigured.

    Distinct from a malformed response because the handling differs: this is
    retried with backoff indefinitely, and the document stays fully searchable
    in the meantime (REQ-055).
    """


@dataclass(frozen=True)
class AIConfig:
    """Which key, model and prompt version are in force right now."""

    api_key: str | None
    model: str = DEFAULT_MODEL
    prompt_version: str = DEFAULT_PROMPT_VERSION

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


async def resolve(session: Any | None = None) -> AIConfig:
    """What the archive is configured to use, settings first, environment second.

    Read on every call rather than cached: a key pasted into Settings has to
    take effect on the next document, not on the next restart. The cost is one
    indexed lookup against a call that takes seconds.
    """
    from api.config import get_settings

    settings = get_settings()
    api_key: str | None = settings.anthropic_api_key
    model = settings.bindery_model
    prompt_version = settings.bindery_prompt_version

    if session is not None:
        from api import settings_store

        api_key = await settings_store.get(session, settings_store.ANTHROPIC_API_KEY)
        model = await settings_store.get(session, settings_store.BINDERY_MODEL) or model
        prompt_version = (
            await settings_store.get(session, settings_store.PROMPT_VERSION) or prompt_version
        )

    return AIConfig(api_key=api_key or None, model=model, prompt_version=prompt_version)


def build_client(api_key: str | None) -> Any | None:
    """An authenticated client, or `None` when there is no key.

    `None` rather than a client that raises at the first call, because "there is
    no key" is a *normal* state here (invariant 7) and every caller has
    something sensible to do with it: the pipeline defers, Ask returns the
    matching pages with no prose, and Settings says so on the panel.

    Imported lazily so that `api/` keeps starting without the SDK installed.
    """
    if not api_key:
        return None

    import anthropic

    return anthropic.AsyncAnthropic(api_key=api_key)


# Anthropic signals an exhausted balance with a 400 and this wording. Matching
# on the message is unpleasant and there is no code to match on instead; the
# check is deliberately narrow, and a miss costs the old behaviour rather than a
# new one.
_BILLING_SIGNATURES = ("credit balance is too low", "billing", "purchase credits")


def is_billing(exc: Exception) -> bool:
    return any(signature in str(exc).lower() for signature in _BILLING_SIGNATURES)


def translate(exc: Exception) -> AIProviderError:
    """Turn an SDK exception into the one taxonomy the retry policy reads.

    Returned rather than raised, so a caller can `raise translate(exc) from exc`
    and keep the original traceback attached — which is the part worth having.

    Anything unrecognised comes back as `AIProviderError`: the conservative
    answer, because an unknown failure retried forever holds a worker slot
    while nothing changes.
    """
    import anthropic

    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderUnavailableError(f"could not reach the API: {exc}")
    if isinstance(exc, anthropic.RateLimitError):
        return ProviderUnavailableError(f"rate limited: {exc}")
    if isinstance(exc, anthropic.APIStatusError):
        # 4xx that is not rate limiting is a request problem, not an outage:
        # retrying unchanged will not help, so it fails loudly.
        if exc.status_code >= 500:
            return ProviderUnavailableError(f"upstream error {exc.status_code}")
        # Checked on the status as well as the class. The SDK raises
        # `RateLimitError` for a 429 today; reading only the class would make
        # this depend on that staying true, and getting it wrong turns a
        # throttle — the one thing waiting definitely fixes — into a dead letter.
        if exc.status_code == 429:
            return ProviderUnavailableError(f"rate limited: {exc}")
        if is_billing(exc):
            # Except this one. An exhausted credit balance arrives as a 400, but
            # retrying unchanged is *exactly* what will work — once somebody
            # tops the account up. Treating it as a bad request dead-letters
            # every document in the queue over about four minutes and presents a
            # billing problem as a corpus problem.
            return ProviderUnavailableError(f"the Anthropic account cannot be billed: {exc}")
        return AIProviderError(f"API rejected the request ({exc.status_code}): {exc}")
    return AIProviderError(f"{type(exc).__name__}: {exc}")
