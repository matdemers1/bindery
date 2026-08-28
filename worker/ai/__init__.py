"""AI provider selection.

The provider is chosen once, from configuration. Nothing downstream knows which
one it got — that is the whole point of ADR-003's adapter.
"""

import logging

from api.config import get_settings
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


def get_provider() -> AIProvider:
    if _override is not None:
        return _override

    settings = get_settings()
    if not settings.anthropic_api_key:
        # Not an error. Retrieval never depends on the API being reachable
        # (invariant 7), so an unconfigured archive is a working archive.
        return UnavailableProvider(prompt_version=settings.bindery_prompt_version)

    from worker.ai.claude import ClaudeProvider

    return ClaudeProvider(
        api_key=settings.anthropic_api_key,
        model=settings.bindery_model,
        prompt_version=settings.bindery_prompt_version,
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
