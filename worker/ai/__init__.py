"""AI provider selection.

The provider is chosen once, from configuration. Nothing downstream knows which
one it got — that is the whole point of ADR-003's adapter.
"""

import logging

from api import ai_client
from worker.ai.provider import (
    AIProvider,
    AIProviderError,
    Candidate,
    ClassificationRequest,
    ClassificationResponse,
    ClassificationResult,
    ProviderUnavailableError,
)
from worker.ai.recorded import RecordedProvider, UnavailableProvider

log = logging.getLogger("bindery.worker.ai")

_override: AIProvider | None = None


def set_provider(provider: AIProvider | None) -> None:
    """Install a provider for the process. Used by tests and by the CLI."""
    global _override
    _override = provider


async def get_provider(session=None) -> AIProvider:
    """Resolve the provider, preferring settings a human set over the environment.

    Reads the database on every call rather than caching, so adding a key in the
    UI takes effect on the next document instead of on the next restart. The
    cost is one indexed lookup per classification, against a call that takes
    seconds.
    """
    if _override is not None:
        return _override

    config = await ai_client.resolve(session)

    if not config.configured:
        # Not an error. Retrieval never depends on the API being reachable
        # (invariant 7), so an unconfigured archive is a working archive.
        return UnavailableProvider(prompt_version=config.prompt_version)

    from worker.ai.claude import ClaudeProvider

    return ClaudeProvider(
        api_key=config.api_key or "",
        model=config.model,
        prompt_version=config.prompt_version,
    )


__all__ = [
    "AIProvider",
    "AIProviderError",
    "Candidate",
    "ClassificationRequest",
    "ClassificationResponse",
    "ClassificationResult",
    "ProviderUnavailableError",
    "RecordedProvider",
    "UnavailableProvider",
    "get_provider",
    "set_provider",
]
