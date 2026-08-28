"""Embed: give each document a vector for neighbour retrieval (REQ-062).

Cheap, local, and deterministic (ADR-007), so replaying this stage over the
whole archive after an embedding change costs CPU and nothing else.
"""

import logging

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api import queue
from api.db.enums import JobStage
from api.db.models import Document, Page
from api.embedding import get_embedding_provider
from api.queue import ClaimedJob
from api.segments import live

log = logging.getLogger("bindery.worker.embed")

# Enough text to characterise the document without letting a 50-page policy
# swamp the vector with boilerplate.
MAX_CHARS = 20000


async def _document_text(session: AsyncSession, document: Document) -> str:
    rows = (
        await session.execute(
            sa.select(Page.text)
            .where(
                Page.source_file_id == document.source_file_id,
                Page.page_number >= document.page_start,
                Page.page_number <= document.page_end,
            )
            .order_by(Page.page_number)
        )
    ).scalars().all()
    return "\n".join(text or "" for text in rows)[:MAX_CHARS]


async def run_embed(session: AsyncSession, job: ClaimedJob) -> None:
    """Embed every live document of a source file, then queue classification."""
    documents = list(
        (
            await session.execute(
                sa.select(Document)
                .where(Document.source_file_id == job.source_file_id, live())
                .order_by(Document.page_start)
            )
        ).scalars().all()
    )
    if not documents:
        log.info("no live documents for %s; nothing to embed", job.source_file_id)
        return

    provider = get_embedding_provider()
    for document in documents:
        document.embedding = provider.embed(await _document_text(session, document))
        # Both keys: the document is what gets classified, but carrying the
        # file as well is what lets every "show me this library's jobs" query
        # reach it by the obvious route.
        await queue.enqueue(
            session,
            JobStage.CLASSIFY,
            document_id=document.id,
            source_file_id=document.source_file_id,
        )
    await session.flush()

    log.info("embedded %s document(s) with %s", len(documents), provider.name)
