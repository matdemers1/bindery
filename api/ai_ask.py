"""The Q&A contract and its Claude implementation (T-8.1, REQ-116).

This lives in `api/` rather than `worker/` for two reasons. The layering rule —
`worker` may import `api`, never the reverse — is the formal one. The real one
is that Q&A is a synchronous request a person is waiting on, not a pipeline
stage: the worker has no use for it.

Citations come from the API's own citations feature rather than from asking the
model to write page numbers into its prose. The distinction matters: a model can
write a plausible page number for a page it never read, and the returned
citation objects index into the exact blocks that were sent, so the mapping back
to a page is a lookup rather than a guess.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from api import ai_client

log = logging.getLogger("bindery.ask")

ASK_MAX_TOKENS = 8000

# Omitting `thinking` runs *adaptive* on current models, and adaptive expands to
# fill `max_tokens`. At the old 2,000 the reasoning could consume the whole
# budget and return an empty answer — which this module then discards as
# uncited, so the reader would be told "I could not find that" about a question
# the archive could answer. Low effort with room to spare keeps the citations
# careful without the latency of a long think.
ASK_THINKING = {"type": "adaptive"}
ASK_EFFORT = {"effort": "low"}

# For structured list work — see `ClaudeAnswerer.complete`.
NO_THINKING = {"type": "disabled"}
LOW_EFFORT = {"effort": "low"}

ASK_SYSTEM = """You answer questions about a person's own document archive.

Rules, in order of importance:

1. Answer only from the supplied documents. If they do not contain the answer,
   say so plainly. A wrong answer about someone's medical or financial records
   is far worse than "I could not find that".
2. Cite every factual claim. An uncited answer is discarded.
3. Be brief and concrete. Give the date, the amount, the name — not a summary of
   what kind of document it was.
4. Never infer beyond the text. A receipt showing a brake pad replacement does
   not mean the brakes are currently in good condition.
"""


@dataclass
class AskSource:
    """One page offered to the model as evidence.

    Carries the ids needed to turn a citation back into a link, because a
    citation the reader cannot follow is barely better than none.
    """

    document_id: str
    source_file_id: str
    title: str
    page_number: int
    text: str


@dataclass
class Citation:
    """Where one sentence of an answer came from."""

    document_id: str
    source_file_id: str
    title: str
    page_number: int
    quote: str


@dataclass
class AskRequest:
    question: str
    sources: list[AskSource]


@dataclass
class AskResponse:
    answer: str
    citations: list[Citation]
    model: str
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def is_cited(self) -> bool:
        return bool(self.citations)


def build_blocks(request: AskRequest) -> list[dict]:
    """Each page becomes its own citable document block.

    One block per page rather than one per document, because the citation a
    reader needs is "page 7 of that scan", and the API cites the block it was
    given. Splitting here is what makes the page number exact.
    """
    return [
        {
            "type": "document",
            "source": {"type": "text", "media_type": "text/plain", "data": source.text},
            "title": f"{source.title} — page {source.page_number}",
            "context": f"document_id={source.document_id} page={source.page_number}",
            "citations": {"enabled": True},
        }
        for source in request.sources
    ]


class TruncatedAnswerError(RuntimeError):
    """The model ran out of output budget mid-answer.

    Distinct from a malformed answer, because the fix is different: raise the
    budget or ask for less, rather than look at the parser.
    """


class ClaudeAnswerer:
    """Answers questions over supplied pages, with citations."""

    def __init__(
        self, api_key: str, model: str = ai_client.DEFAULT_MODEL, client: Any = None
    ) -> None:
        self.model = model
        # Built through `api/ai_client.py` rather than here. This class cannot be
        # an `AIProvider` — that protocol lives in `worker/`, which `api/` may
        # not import — but "how a client is made" is not the part that needed
        # to differ, and having it in two places is what ADR-003 is against
        # (BND-FR-002).
        self._client = client if client is not None else ai_client.build_client(api_key)

    def available(self) -> bool:
        return self._client is not None

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 4000,
        thinking: dict | None = None,
        effort: dict | None = None,
    ) -> str:
        """A plain completion, for asking about the archive's own structure.

        Used by the unification pass, which sends a list of taxonomy names and
        no document content at all.

        A truncated answer is raised rather than returned. JSON cut off mid
        string is *invalid* JSON, so the caller would otherwise report "the
        answer could not be read" — which is true, and sends you looking at the
        parser instead of at `max_tokens`. That is exactly what happened on the
        first run over 277 document types.

        Thinking is **off** by default here, which is not a cost decision.
        Measured on a 200-name grouping task against the deployed archive:

        | thinking             | thinking tokens | groups found | wall clock |
        |----------------------|----------------:|-------------:|-----------:|
        | adaptive, effort low |             878 |            1 |        10s |
        | adaptive, medium     |          11,476 |            2 |       118s |
        | disabled             |               0 |       **11** |     **8s** |

        Omitting the parameter runs *adaptive* on this model, and adaptive
        expands to fill the budget it is given — the first attempt spent all
        16,000 tokens reasoning and returned an empty string. The interesting
        row is the middle one: even where thinking left room for an answer, it
        produced a worse one. Grouping names by similarity is recognition, not
        deduction, and reasoning talks the model out of groupings it can see.
        """
        if self._client is None:
            raise RuntimeError("no Anthropic API key configured")
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            thinking=thinking or NO_THINKING,
            # Bounded even though the default turns thinking off entirely: a
            # caller that passes `thinking` and finds no ceiling has rediscovered
            # the bug this parameter exists to prevent. Adaptive thinking expands
            # to fill `max_tokens`.
            output_config=effort or LOW_EFFORT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        if getattr(response, "stop_reason", None) == "max_tokens":
            raise TruncatedAnswerError(
                f"the answer was cut off at {max_tokens} tokens "
                f"({len(text)} characters returned)"
            )
        return text

    async def answer(self, request: AskRequest) -> AskResponse:
        if self._client is None:
            raise RuntimeError("no Anthropic API key configured")
        if not request.sources:
            # Nothing to cite means nothing can be answered, and calling the API
            # to be told so wastes money and offers a chance to hallucinate.
            return AskResponse("", [], self.model)

        response = await self._client.messages.create(
            model=self.model,
            max_tokens=ASK_MAX_TOKENS,
            thinking=ASK_THINKING,
            output_config=ASK_EFFORT,
            system=ASK_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": [*build_blocks(request), {"type": "text", "text": request.question}],
                }
            ],
        )

        text_parts: list[str] = []
        citations: list[Citation] = []
        for block in response.content:
            if getattr(block, "type", None) != "text":
                continue
            text_parts.append(block.text)
            for citation in getattr(block, "citations", None) or []:
                index = getattr(citation, "document_index", None)
                if index is None or not 0 <= index < len(request.sources):
                    # A citation pointing outside what was sent is not evidence.
                    log.warning("dropping citation with out-of-range index %r", index)
                    continue
                source = request.sources[index]
                citations.append(
                    Citation(
                        document_id=source.document_id,
                        source_file_id=source.source_file_id,
                        title=source.title,
                        page_number=source.page_number,
                        quote=(getattr(citation, "cited_text", "") or "").strip(),
                    )
                )

        usage = response.usage.model_dump() if hasattr(response.usage, "model_dump") else {}
        # Deliberately not the question. A search or a Q&A query is among the
        # most revealing things a person types — "what is my policy number",
        # "when was my diagnosis" — and this line was globally visible in the
        # event log (REQ-144). The shape is enough to debug retrieval.
        log.info(
            "answered a %s-character question with %s citations from %s pages",
            len(request.question), len(citations), len(request.sources),
        )
        return AskResponse(
            answer="".join(text_parts).strip(),
            citations=citations,
            model=self.model,
            usage=usage,
        )
