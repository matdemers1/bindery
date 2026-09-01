"""Searching an unlocked vault (T-16.8, REQ-184).

Decrypt and scan, in this process, while unlocked. Deliberately **not** a
Postgres index: a plaintext FTS table populated on unlock performs far better
and writes the text to disk, so a crash while unlocked leaves exactly what the
vault exists to prevent sitting in a table nobody remembers to clear.

The cost is real and bounded. This is linear in the size of the vault, against
~15 ms Postgres FTS for the main archive at 100K pages. That is fine for tens or
hundreds of documents and is not fine for thousands, so the ceiling is measured
rather than assumed and reported rather than discovered.
"""

import logging
import re
import time
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import VaultItem, VaultPage
from api.vault import crypto, store

log = logging.getLogger("bindery.vault")

# Past this, the scan is slow enough that a person notices, and the honest
# response is to say so rather than to quietly take four seconds.
SLOW_AFTER_PAGES = 5_000
SNIPPET_RADIUS = 90


@dataclass
class VaultHit:
    document_id: uuid.UUID
    title: str | None
    page_number: int
    snippet: str


@dataclass
class VaultResults:
    total: int
    hits: list[VaultHit]
    pages_scanned: int
    elapsed_ms: int
    # Set when the vault has grown past the point where scanning it is quick.
    # Surfaced rather than logged: a search that got slower every month with
    # nothing saying why is how people conclude the archive is broken.
    slow: bool = False


def _snippet(text: str, terms: list[str]) -> str:
    lowered = text.lower()
    at = min(
        (lowered.find(term) for term in terms if lowered.find(term) >= 0),
        default=-1,
    )
    if at < 0:
        return text[: SNIPPET_RADIUS * 2].strip()
    start = max(0, at - SNIPPET_RADIUS)
    end = min(len(text), at + SNIPPET_RADIUS)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


async def search(
    session: AsyncSession,
    vault_id: uuid.UUID,
    query: str,
    data_key: bytes,
    *,
    limit: int = 25,
) -> VaultResults:
    """Every vaulted page matching the query, decrypted here and now."""
    terms = [term for term in re.split(r"\W+", query.lower()) if len(term) > 1]
    if not terms:
        return VaultResults(total=0, hits=[], pages_scanned=0, elapsed_ms=0)

    started = time.monotonic()
    rows = (
        await session.execute(
            sa.select(VaultPage, VaultItem)
            .join(VaultItem, VaultItem.id == VaultPage.vault_item_id)
            .where(VaultItem.vault_id == vault_id)
            .order_by(VaultItem.vaulted_at.desc(), VaultPage.page_number)
        )
    ).all()

    hits: list[VaultHit] = []
    titles: dict[uuid.UUID, str | None] = {}
    for page, item in rows:
        try:
            text = crypto.decrypt(page.sealed_text, data_key).decode()
        except crypto.WrongSecret:
            # One unreadable page must not take the whole search down, and it
            # must not pass unmentioned either (invariant 8).
            log.error("a vaulted page for item %s did not decrypt", item.id)
            continue
        lowered = text.lower()
        if not all(term in lowered for term in terms):
            continue
        if item.id not in titles:
            titles[item.id] = store.open_meta(item, data_key).get("title")
        hits.append(
            VaultHit(
                document_id=item.document_id,
                title=titles[item.id],
                page_number=page.page_number,
                snippet=_snippet(text, terms),
            )
        )

    elapsed = int((time.monotonic() - started) * 1000)
    if len(rows) > SLOW_AFTER_PAGES:
        log.warning(
            "vault search scanned %s pages in %sms — past %s pages this is slow "
            "enough to notice, and the design note in ADR-012 applies",
            len(rows), elapsed, SLOW_AFTER_PAGES,
        )
    return VaultResults(
        total=len(hits),
        hits=hits[:limit],
        pages_scanned=len(rows),
        elapsed_ms=elapsed,
        slow=len(rows) > SLOW_AFTER_PAGES,
    )
