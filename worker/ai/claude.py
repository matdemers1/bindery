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

import base64
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
    ProviderRefusedError,
    ProviderUnavailableError,
)

log = logging.getLogger("bindery.worker.ai")

PROMPT_DIR = Path(__file__).parent / "prompts"
# Bounded so a 300-page bundle cannot produce a single enormous request. The
# first pages carry the identifying content; the tail is continuation.
MAX_PAGES = 12
MAX_CHARS_PER_PAGE = 6000
# Adaptive thinking expands to fill this, so it has to leave room for an answer
# after the reasoning. At 8,000 the fallback model would think its way through
# the whole budget on a long document and return nothing — reported, unhelpfully,
# as "structured output was empty".
MAX_TOKENS = 16000

# Asked once when the configured model refuses. A safety classifier declining an
# ordinary mortgage deed is a false positive, and Sonnet does exactly that to a
# VA security deed that Opus reads without complaint. Sonnet 5 cannot use the
# server-side `fallbacks` parameter — it is Opus/Fable only — so the escalation
# is made here.
REFUSAL_FALLBACK_MODEL = "claude-opus-5"
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
        if candidate.usage_label is not None:
            seen = f"  · {candidate.usage_label}" if candidate.usage_label else ""
        elif candidate.usage_count:
            seen = (
                f"  · used by {candidate.usage_count} document"
                f"{'s' if candidate.usage_count != 1 else ''}"
            )
        else:
            seen = ""
        lines.append(f"- `{candidate.id}` — {candidate.name}{seen}")
    return "\n".join(lines) + "\n"


def build_candidate_block(request: ClassificationRequest) -> str:
    """The stable half of the prompt: instructions plus what the archive knows."""
    return "\n".join([
        "## Candidates from this archive",
        "",
        f"Ranked by use across {request.candidates_ranked_by}. Prefer one of these "
        "over a new name unless none of them describes this document. A near-duplicate "
        "of an entry already here is worse than an imperfect match to it.",
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


# Anthropic signals an exhausted balance with a 400 and this wording. Matching
# on the message is unpleasant and there is no code to match on instead; the
# check is deliberately narrow, and a miss costs the old behaviour rather than a
# new one.
_BILLING_SIGNATURES = ("credit balance is too low", "billing", "purchase credits")


def _is_billing(exc: Exception) -> bool:
    return any(signature in str(exc).lower() for signature in _BILLING_SIGNATURES)


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

        # When OCR read nothing, the text block says so and nothing else. Looking
        # at the page is then the only way anything true can be said about it —
        # a squadron patch, a photograph, a hand-drawn diagram. The images go
        # after the text so the instructions and candidates stay cacheable.
        if request.page_images:
            user_content = [
                {"type": "text", "text": user_content},
                *(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": page.media_type,
                            "data": base64.b64encode(page.data).decode(),
                        },
                    }
                    for page in request.page_images
                ),
                {
                    "type": "text",
                    "text": (
                        "The page text above is empty because OCR found no readable "
                        "text. Describe what these page images actually show and "
                        "classify from that. Say what is visibly there — do not "
                        "infer an issuer, a date or an account from a design."
                    ),
                },
            ]

        response, effective_model = await self._parse_with_fallback(
            system=system, user_content=user_content, document_id=request.document_id
        )

        result = getattr(response, "parsed_output", None)
        if result is None:
            # Never just "empty". That message cost three wrong diagnoses on one
            # document: first a suspected token limit, then a refusal that was
            # genuinely there, then a *real* token limit on the fallback model —
            # all wearing the same six words. The stop reason distinguishes them
            # and is free to include.
            stop = getattr(response, "stop_reason", "unknown")
            raise AIProviderError(
                f"the model returned no structured output (stop_reason={stop})."
                + (
                    " The answer was cut off — raise MAX_TOKENS."
                    if stop == "max_tokens"
                    else ""
                )
            )

        usage = response.usage.model_dump() if hasattr(response.usage, "model_dump") else {}
        cache_read = usage.get("cache_read_input_tokens") or 0
        log.info(
            "classified %s on %s: %s input / %s output tokens, %s read from cache",
            request.document_id, effective_model,
            usage.get("input_tokens"), usage.get("output_tokens"), cache_read,
        )

        return ClassificationResponse(
            result=result,
            # The model that actually answered, not the one that was asked.
            # Spend is costed per model and provenance is displayed, so a
            # fallback that reported the configured model would make both wrong.
            model=effective_model,
            prompt_version=self.prompt_version,
            usage=usage,
            raw_request={
                "system": system,
                "user": user_content,
                "model": effective_model,
                "prompt_version": self.prompt_version,
            },
            raw_response=json.loads(result.model_dump_json()),
        )

    async def _parse_with_fallback(self, *, system, user_content, document_id):
        """Ask the configured model; on a refusal, ask a more capable one once.

        A safety classifier declining an ordinary mortgage deed is a false
        positive, and it is not hypothetical — `general_harms` on a VA security
        deed is what sent this document to dead-letter five times. Sonnet
        refuses it; Opus classifies it as a Uniform Residential Loan
        Application, which is what it is.

        One escalation, not a ladder. If the more capable model declines too,
        that is an answer rather than a reason to keep asking.
        """
        attempts = [self.model]
        if REFUSAL_FALLBACK_MODEL != self.model:
            attempts.append(REFUSAL_FALLBACK_MODEL)

        last_refusal = None
        for index, model in enumerate(attempts):
            response = await self._parse_once(
                model=model, system=system, user_content=user_content
            )
            if getattr(response, "stop_reason", None) != "refusal":
                if index:
                    log.warning(
                        "document %s was refused by %s and accepted by %s",
                        document_id, attempts[0], model,
                    )
                return response, model

            details = getattr(response, "stop_details", None)
            last_refusal = getattr(details, "category", None) or "unspecified"
            log.warning(
                "%s refused document %s (%s)", model, document_id, last_refusal
            )

        raise ProviderRefusedError(
            f"the model declined to classify this document ({last_refusal}). "
            "This is a safety refusal, not a problem with the file — the "
            "document is stored, searchable and unchanged; only the automatic "
            "title and tags are missing. Classify it by hand from the review "
            "screen.",
            category=last_refusal,
        )

    async def _parse_once(self, *, model: str, system, user_content):
        try:
            return await self._client.messages.parse(
                model=model,
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
            if _is_billing(exc):
                # Except this one. An exhausted credit balance arrives as a 400,
                # but retrying unchanged is *exactly* what will work — once
                # somebody tops the account up. Treating it as a bad request
                # dead-letters every document in the queue over about four
                # minutes and presents a billing problem as a corpus problem.
                raise ProviderUnavailableError(
                    f"the Anthropic account cannot be billed: {exc}"
                ) from exc
            raise AIProviderError(f"API rejected the request ({exc.status_code}): {exc}") from exc
        except ValidationError as exc:
            raise AIProviderError(f"response did not match the schema: {exc}") from exc

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

