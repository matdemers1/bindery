"""Q&A over the archive, every answer cited to a page (T-8.1, REQ-116).

> An uncited answer is a failure, not a degraded result.

That is the rule this module is built around, and it is not stylistic. An LLM
confidently summarising someone's VA records without showing its source is the
exact failure the whole project's trust principle exists to prevent — and it is
worse than no feature at all, because a plausible wrong answer is acted on.

Three structural defences:

**Retrieval never depends on the API** (invariant 7). Candidate pages are found
by the same Postgres full-text search that powers the search box. If Claude is
unreachable, this endpoint degrades to *"here are the pages that mention it"* —
which is still useful, and is exactly what the search box would have given you.

**The model only sees pages the caller can see.** Sources are drawn from a
library-scoped search, so the leak suite's guarantees cover this path too.

**An uncited answer is discarded.** Not flagged, not shown with a caveat —
discarded, and replaced with the retrieved pages. Showing an uncited answer with
a warning is still showing it, and warnings are read exactly once.
"""

import logging
import re
import uuid
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.ai_ask import AskRequest, AskResponse, AskSource, Citation
from api.db.models import Document, Page, SourceFile
from api.search import query as search_query

log = logging.getLogger("bindery.ask")

# Enough pages to answer a real question, few enough to stay inside a sensible
# context and a sensible cost. Retrieval ranking is doing the real work.
MAX_SOURCE_PAGES = 20
MAX_PAGE_CHARS = 6000


# Words that carry no retrieval signal. Postgres strips these from the *index*,
# but `websearch_to_tsquery` still ANDs them into the query, so "when did I last
# get the brakes done?" becomes a query no receipt can satisfy.
BROAD_TERMS = 4

STOPWORDS = frozenset("""
a an and are as at be been but by can could did do does for from get got had has
have how i if in into is it its last me my of on or our out show tell that the
their them then there these they this to was were what when where which who whom
why will with would you your
""".split())


def question_to_query(question: str) -> tuple[str, str]:
    """Turn a question into a precise query and a broad fallback.

    Retrieval is the part that must not fail, so it gets two attempts: the
    content words ANDed, which is precise and often empty, then the same words
    ORed, which is what actually answers "when did I last get the brakes done?"
    — the only words in that sentence a receipt contains are *brakes*.
    """
    words = [
        word
        for word in re.findall(r"[\w'-]+", question.lower())
        if len(word) > 1 and word not in STOPWORDS
    ]
    seen: list[str] = []
    for word in words:
        if word not in seen:
            seen.append(word)
    if not seen:
        # Nothing but stopwords. Fall back to the question as written rather
        # than to an empty query, which would match everything.
        return question.strip(), question.strip()

    # The OR fallback is the expensive shape: every extra term is another slice
    # of the archive to rank. Longer words are a decent proxy for rarer ones —
    # "brakes" discriminates, "cost" does not — so the broad pass uses the few
    # most selective terms rather than all of them. Precision is what makes it
    # fast *and* what makes it useful; an OR over nine words returns the whole
    # archive in relevance order, which is not an answer.
    selective = sorted(seen, key=len, reverse=True)[:BROAD_TERMS]
    return " ".join(seen), " OR ".join(
        [word for word in seen if word in set(selective)]
    )



@dataclass
class AskResult:
    question: str
    answer: str | None
    citations: list[Citation] = field(default_factory=list)
    # Always populated, so the screen has something true to show even when the
    # model could not be reached.
    consulted: list[dict] = field(default_factory=list)
    unavailable_reason: str | None = None
    model: str | None = None

    def as_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "citations": [
                {
                    "document_id": citation.document_id,
                    "source_file_id": citation.source_file_id,
                    "title": citation.title,
                    "page_number": citation.page_number,
                    "quote": citation.quote,
                }
                for citation in self.citations
            ],
            "consulted": self.consulted,
            "unavailable_reason": self.unavailable_reason,
            "model": self.model,
        }


async def gather_sources(
    session: AsyncSession,
    question: str,
    library_ids: list[uuid.UUID],
    viewer: uuid.UUID | None = None,
) -> list[AskSource]:
    """Find the pages worth reading, using search rather than the model.

    `viewer` carries the vault boundary. Ask reads page *text* and hands it to
    a model, so a vaulted document reaching here would not merely be listed —
    it would be quoted back, and sent to Anthropic.
    """
    precise, broad = question_to_query(question)
    response = await search_query.search(
        session, precise, library_ids, viewer=viewer, limit=MAX_SOURCE_PAGES
    )
    if not response.results and broad != precise:
        log.debug("broadening %r to an OR query", question[:60])
        response = await search_query.search(
            session, broad, library_ids, viewer=viewer, limit=MAX_SOURCE_PAGES
        )
    if not response.results:
        return []

    document_ids = [result.document_id for result in response.results]
    rows = (
        await session.execute(
            sa.select(
                Document.id, Document.title, Document.source_file_id,
                Page.page_number, Page.text, SourceFile.original_filename,
            )
            .join(SourceFile, SourceFile.id == Document.source_file_id)
            .join(
                Page,
                sa.and_(
                    Page.source_file_id == Document.source_file_id,
                    Page.page_number.between(Document.page_start, Document.page_end),
                ),
            )
            .where(
                Document.id.in_(document_ids),
                Page.text.is_not(None),
                # The library filter is applied by search above; repeating it
                # here is deliberate belt-and-braces on the one path that hands
                # raw page text to a third party.
                Document.library_id.in_(library_ids),
            )
            .order_by(Document.id, Page.page_number)
        )
    ).all()

    # Preserve the search ranking: the best-matching document's pages first.
    order = {document_id: index for index, document_id in enumerate(document_ids)}
    sources = [
        AskSource(
            document_id=str(row.id),
            source_file_id=str(row.source_file_id),
            title=row.title or row.original_filename or "Untitled",
            page_number=row.page_number,
            text=(row.text or "")[:MAX_PAGE_CHARS],
        )
        for row in sorted(rows, key=lambda r: (order.get(r.id, 999), r.page_number))
        if (row.text or "").strip()
    ]
    return sources[:MAX_SOURCE_PAGES]


def _consulted(sources: list[AskSource]) -> list[dict]:
    return [
        {
            "document_id": source.document_id,
            "source_file_id": source.source_file_id,
            "title": source.title,
            "page_number": source.page_number,
        }
        for source in sources
    ]


async def ask(
    session: AsyncSession,
    question: str,
    library_ids: list[uuid.UUID],
    provider,
    viewer: uuid.UUID | None = None,
) -> AskResult:
    """Answer a question, or say honestly why there is no answer."""
    sources = await gather_sources(session, question, library_ids, viewer)
    consulted = _consulted(sources)

    if not sources:
        return AskResult(
            question=question,
            answer=None,
            unavailable_reason="Nothing in the archive matches that question.",
        )

    if provider is None or not provider.available():
        # Degrade to retrieval, which is the thing that was never allowed to
        # depend on the API in the first place.
        return AskResult(
            question=question,
            answer=None,
            consulted=consulted,
            unavailable_reason=(
                "No API key is configured, so I cannot write an answer — but "
                "these pages mention it."
            ),
        )

    try:
        response: AskResponse = await provider.answer(
            AskRequest(question=question, sources=sources)
        )
    except Exception as error:
        log.error("ask failed: %s", error)
        return AskResult(
            question=question,
            answer=None,
            consulted=consulted,
            unavailable_reason=f"Could not reach the model ({error}). These pages mention it.",
        )

    if not response.is_cited:
        # The rule, enforced. An uncited answer is discarded rather than shown
        # with a caveat — a caveat is read once and an answer is believed.
        log.warning("discarding an uncited answer to %r", question[:60])
        return AskResult(
            question=question,
            answer=None,
            consulted=consulted,
            unavailable_reason=(
                "I could not find a passage that actually supports an answer, so "
                "I am not going to give you one. These pages mention it."
            ),
            model=response.model,
        )

    return AskResult(
        question=question,
        answer=response.answer,
        citations=response.citations,
        consulted=consulted,
        model=response.model,
    )
