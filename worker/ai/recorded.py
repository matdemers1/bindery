"""A provider that replays recorded responses.

> AI calls are never live in the default test run — otherwise the test suite has
> a bill and a flake rate.

Also the deliberate stand-in when no API key is configured: it reports itself
unavailable, which is what makes REQ-055 true — a document ingested with no
provider is still OCR'd, paged, segmented and fully searchable, and only its
classification defers.
"""

import json
from pathlib import Path
from typing import Any

from worker.ai.provider import (
    AIProviderError,
    BoundaryConfirmation,
    BoundaryRequest,
    BoundaryVerdict,
    ClassificationRequest,
    ClassificationResponse,
    ClassificationResult,
    ProviderUnavailableError,
)


class UnavailableProvider:
    """No provider configured. Everything except classification still works."""

    name = "unavailable"

    def __init__(self, model: str = "none", prompt_version: str = "v1") -> None:
        self.model = model
        self.prompt_version = prompt_version

    def available(self) -> bool:
        return False

    async def classify(self, request: ClassificationRequest) -> ClassificationResponse:
        raise ProviderUnavailableError(
            "no AI provider is configured; classification is deferred"
        )

    async def confirm_boundaries(self, request: BoundaryRequest) -> BoundaryConfirmation:
        raise ProviderUnavailableError("no AI provider is configured")


class RecordedProvider:
    """Replays fixtures keyed by document id, or a single default response."""

    name = "recorded"

    def __init__(
        self,
        responses: dict[str, dict[str, Any]] | None = None,
        default: dict[str, Any] | None = None,
        model: str = "recorded",
        prompt_version: str = "v1",
        usage: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.prompt_version = prompt_version
        self._responses = responses or {}
        self._default = default
        self._usage = usage or {
            "input_tokens": 1200,
            "output_tokens": 180,
            "cache_read_input_tokens": 900,
        }
        # Every request the provider saw, so tests can assert on what the prompt
        # actually contained (REQ-047).
        self.requests: list[ClassificationRequest] = []
        self.boundary_requests: list[BoundaryRequest] = []
        # page -> verdict. Anything not listed is treated as "not a boundary",
        # which is the conservative answer.
        self.boundary_verdicts: dict[int, bool] = {}

    @classmethod
    def from_directory(cls, directory: Path, **kwargs) -> "RecordedProvider":
        responses = {
            path.stem: json.loads(path.read_text()) for path in directory.glob("*.json")
        }
        return cls(responses=responses, **kwargs)

    def available(self) -> bool:
        return True

    async def classify(self, request: ClassificationRequest) -> ClassificationResponse:
        self.requests.append(request)
        payload = self._responses.get(request.document_id, self._default)
        if payload is None:
            raise AIProviderError(f"no recorded response for {request.document_id}")

        # Validated exactly as a live response would be, so a fixture that would
        # not survive the real schema fails here rather than passing quietly.
        result = ClassificationResult.model_validate(payload)
        return ClassificationResponse(
            result=result,
            model=self.model,
            prompt_version=self.prompt_version,
            usage=dict(self._usage),
            raw_request={"recorded": True, "document_id": request.document_id},
            raw_response=payload,
        )

    async def confirm_boundaries(self, request: BoundaryRequest) -> BoundaryConfirmation:
        self.boundary_requests.append(request)
        return BoundaryConfirmation(
            verdicts=[
                BoundaryVerdict(
                    page=window.page,
                    starts_new_document=self.boundary_verdicts.get(window.page, False),
                    reason="recorded",
                )
                for window in request.windows
            ]
        )
