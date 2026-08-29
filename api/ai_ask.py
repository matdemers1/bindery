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

log = logging.getLogger("bindery.ask")

ASK_MAX_TOKENS = 2000

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


class ClaudeAnswerer:
    """Answers questions over supplied pages, with citations."""

    def __init__(self, api_key: str, model: str = "claude-opus-5", client: Any = None) -> None:
        self.model = model
        self._client = client
        if client is None and api_key:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=api_key)

    def available(self) -> bool:
        return self._client is not None

    async def complete(self, prompt: str, *, max_tokens: int = 4000) -> str:
        """A plain completion, for asking about the archive's own structure.

        Used by the correspondent unification pass, which sends a list of
        folder names and no document content at all.
        """
        if self._client is None:
            raise RuntimeError("no Anthropic API key configured")
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )

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
        log.info(
            "answered %r with %s citations from %s pages",
            request.question[:60], len(citations), len(request.sources),
        )
        return AskResponse(
            answer="".join(text_parts).strip(),
            citations=citations,
            model=self.model,
            usage=usage,
        )
