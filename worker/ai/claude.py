"""Claude behind the adapter (ADR-003).

`claude-opus-5`, adaptive thinking, strict structured output via
`messages.parse` so a malformed response raises rather than arriving as a
plausible-looking dict.

Prompt caching is deliberate, not incidental: the instruction block and the
candidate taxonomy header sit before the last `cache_control` breakpoint, and
the per-document page text after it. Cache health is read back from
`usage.cache_read_input_tokens` and stored on every classification row, so a
silent invalidation shows up as a number rather than as a bill (REQ-053).
"""

import json
import logging
from pathlib import Path
from typing import Any

import anthropic
from pydantic import ValidationError

from worker.ai.provider import (
    AIProviderError,
    BoundaryConfirmation,
    BoundaryRequest,
    Candidate,
    ClassificationRequest,
    ClassificationResponse,
    ClassificationResult,
    ProviderUnavailableError,
)

log = logging.getLogger("bindery.worker.ai")

PROMPT_DIR = Path(__file__).parent / "prompts"
# Bounded so a 300-page bundle cannot produce a single enormous request. The
# first pages carry the identifying content; the tail is continuation.
MAX_PAGES = 12
MAX_CHARS_PER_PAGE = 6000
MAX_TOKENS = 8000
# A seam only needs the pages either side of it, trimmed — the decision is made
# on letterheads and headings, not on body text.
BOUNDARY_CHARS_PER_PAGE = 1500


def load_prompt_named(name: str) -> str:
    path = PROMPT_DIR / f"{name}.md"
    if not path.is_file():
        raise AIProviderError(f"no prompt file named {name!r}")
    return path.read_text()


def load_prompt(version: str) -> str:
    return load_prompt_named(f"classify_{version}")


def _render_candidates(label: str, candidates: list[Candidate]) -> str:
    if not candidates:
        return f"### {label}\n\n(none yet — propose a new name if the document needs one)\n"
    lines = [f"### {label}", ""]
    for candidate in candidates:
        seen = (
            f"  · used by {candidate.neighbour_count} similar document"
            f"{'s' if candidate.neighbour_count != 1 else ''}"
            if candidate.neighbour_count
            else ""
        )
        lines.append(f"- `{candidate.id}` — {candidate.name}{seen}")
    return "\n".join(lines) + "\n"


def build_candidate_block(request: ClassificationRequest) -> str:
    """The stable half of the prompt: instructions plus what the archive knows."""
    return "\n".join([
        "## Candidates from this archive",
        "",
        "Ranked by how many documents similar to this one already use them.",
        "",
        _render_candidates("Correspondents", request.correspondents),
        _render_candidates("Document types", request.document_types),
        _render_candidates("Tags", request.tags),
    ])


def build_document_block(request: ClassificationRequest) -> str:
    """The volatile half: this document's text. Everything after the cache breakpoint."""
    parts: list[str] = []
    if request.known_form_code:
        parts.append(
            f"This document has already been identified as **{request.known_form_code}** "
            "by deterministic fingerprint."
        )
        if request.known_form_fields:
            parts.append(
                "Extract these typed fields into `extracted_fields` where the page text "
                "supports them: " + ", ".join(request.known_form_fields) + "."
            )
        parts.append("")

    parts.append("## Document text")
    parts.append("")
    for page_number, text in request.pages[:MAX_PAGES]:
        parts.append(f"--- page {page_number} ---")
        parts.append(text[:MAX_CHARS_PER_PAGE] or "(no text on this page)")
        parts.append("")
    if len(request.pages) > MAX_PAGES:
        parts.append(
            f"({len(request.pages) - MAX_PAGES} further pages omitted — "
            "they are continuation sheets.)"
        )
    return "\n".join(parts)


class ClaudeProvider:
    name = "claude"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-opus-5",
        prompt_version: str = "v1",
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.prompt_version = prompt_version
        self._api_key = api_key
        self._client = client or (
            anthropic.AsyncAnthropic(api_key=api_key) if api_key else None
        )

    def available(self) -> bool:
        return self._client is not None

    async def classify(self, request: ClassificationRequest) -> ClassificationResponse:
        if self._client is None:
            raise ProviderUnavailableError("no Anthropic API key configured")

        instructions = load_prompt(self.prompt_version)
        # Order matters for caching: instructions and candidates are stable
        # across documents; page text is not. The breakpoint goes between them.
        system = [
            {"type": "text", "text": instructions},
            {
                "type": "text",
                "text": build_candidate_block(request),
                "cache_control": {"type": "ephemeral"},
            },
        ]
        user_content = build_document_block(request)

        try:
            response = await self._client.messages.parse(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                thinking={"type": "adaptive"},
                messages=[{"role": "user", "content": user_content}],
                output_format=ClassificationResult,
            )
        except anthropic.APIConnectionError as exc:
            raise ProviderUnavailableError(f"could not reach the API: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderUnavailableError(f"rate limited: {exc}") from exc
        except anthropic.APIStatusError as exc:
            # 4xx that is not rate limiting is a request problem, not an outage:
            # retrying unchanged will not help, so it fails loudly.
            if exc.status_code >= 500:
                raise ProviderUnavailableError(f"upstream error {exc.status_code}") from exc
            raise AIProviderError(f"API rejected the request ({exc.status_code}): {exc}") from exc
        except ValidationError as exc:
            raise AIProviderError(f"response did not match the schema: {exc}") from exc

        result = getattr(response, "parsed_output", None)
        if result is None:
            # Structured output guarantees this; if it is ever absent the
            # correct response is to fail, not to guess at the content.
            raise AIProviderError("structured output was empty")

        usage = response.usage.model_dump() if hasattr(response.usage, "model_dump") else {}
        cache_read = usage.get("cache_read_input_tokens") or 0
        log.info(
            "classified %s: %s input / %s output tokens, %s read from cache",
            request.document_id, usage.get("input_tokens"), usage.get("output_tokens"), cache_read,
        )

        return ClassificationResponse(
            result=result,
            model=self.model,
            prompt_version=self.prompt_version,
            usage=usage,
            raw_request={
                "system": system,
                "user": user_content,
                "model": self.model,
                "prompt_version": self.prompt_version,
            },
            raw_response=json.loads(result.model_dump_json()),
        )

    async def confirm_boundaries(self, request: BoundaryRequest) -> BoundaryConfirmation:
        """Rule on the seams the heuristics could not settle (REQ-035).

        Every ambiguous candidate for a file goes in one request: they share the
        same instructions, so batching them means one cached prefix instead of
        one per seam, and the model sees the bundle's own conventions rather
        than each seam in isolation.
        """
        if self._client is None:
            raise ProviderUnavailableError("no Anthropic API key configured")
        if not request.windows:
            return BoundaryConfirmation()

        instructions = load_prompt_named(f"boundaries_{self.prompt_version}")
        parts: list[str] = []
        for window in request.windows:
            parts.append(f"## Candidate boundary at page {window.page}")
            parts.append("")
            parts.append(f"Heuristics flagged it because: {'; '.join(window.reasons)}.")
            parts.append("")
            sections = (("Before", window.before), ("From the candidate on", window.after))
            for label, pages in sections:
                parts.append(f"### {label}")
                for number, text in pages:
                    parts.append(f"--- page {number} ---")
                    parts.append(text[:BOUNDARY_CHARS_PER_PAGE] or "(no text on this page)")
                parts.append("")

        try:
            response = await self._client.messages.parse(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": instructions,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                thinking={"type": "adaptive"},
                messages=[{"role": "user", "content": "\n".join(parts)}],
                output_format=BoundaryConfirmation,
            )
        except anthropic.APIConnectionError as exc:
            raise ProviderUnavailableError(f"could not reach the API: {exc}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500 or exc.status_code == 429:
                raise ProviderUnavailableError(f"upstream error {exc.status_code}") from exc
            raise AIProviderError(f"API rejected the request ({exc.status_code})") from exc
        except ValidationError as exc:
            raise AIProviderError(f"response did not match the schema: {exc}") from exc

        confirmation = getattr(response, "parsed_output", None)
        if confirmation is None:
            raise AIProviderError("structured output was empty")
        return confirmation

