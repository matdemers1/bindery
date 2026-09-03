"""Two uploads cannot both spend the last of a quota (CR-076).

`quota.check` is a read followed by a write: `usage_for` is a plain aggregate,
and the `source_file` row that makes the answer true is inserted afterwards.
Without serialisation two requests both read 4.7 GB of a 5 GB limit, both
decide they have room, and both store — and concurrency here is the normal
case, not an edge one. The backlog importer and the watched-folder walker both
ingest in parallel, and a browser posts several files at once.

The consequence is the one `api/quota.py`'s own docstring names: the Zima has
one disk pool, and when it fills, OCR stops, backups stop, and Postgres stops,
for everyone.

`pg_advisory_xact_lock` is what holds. It is taken inside the caller's
transaction and released when that transaction ends — which is *after* the
`source_file` row is inserted — so the second request through reads a total
that already includes the first file.
"""

import asyncio
import uuid

import sqlalchemy as sa

from api import quota
from api.db.enums import IngestSource
from api.db.models import AppUser, SourceFile
from api.db.session import SessionFactory


async def _store(user_id: uuid.UUID, library_id: uuid.UUID, size: int) -> str:
    """One whole upload, in its own transaction: check, then insert, then commit."""
    async with SessionFactory() as own:
        user = await own.get(AppUser, user_id)
        try:
            await quota.check(own, user, size)
        except quota.QuotaExceeded:
            await own.rollback()
            return "refused"
        own.add(
            SourceFile(
                library_id=library_id,
                sha256=(uuid.uuid4().hex * 2)[:64],
                byte_size=size,
                original_filename="scan.pdf",
                ingest_source=IngestSource.WEB_UPLOAD,
            )
        )
        await own.commit()
        return "stored"


async def test_two_concurrent_uploads_cannot_both_fit_in_one_slot(session, signed_in):
    """The whole finding, in one assertion.

    Both files fit on their own and only one fits after the other. Unserialised
    they both read an empty account and both pass; serialised, the second waits
    for the first's transaction and then sees it.
    """
    user, library = await signed_in()
    user.storage_quota_bytes = 1_000
    await session.commit()

    outcomes = await asyncio.gather(
        _store(user.id, library.id, 800),
        _store(user.id, library.id, 800),
    )

    assert sorted(outcomes) == ["refused", "stored"], outcomes

    stored = (
        await session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(SourceFile.byte_size), 0)).where(
                SourceFile.library_id == library.id
            )
        )
    ).scalar_one()
    assert stored <= 1_000, f"the account is holding {stored} bytes of a 1,000 limit"


async def test_the_lock_is_per_account(session, signed_in, user_factory):
    """Two households must never wait on each other, and must never be charged
    for each other's bytes."""
    first, first_library = await signed_in()
    second, second_library = await user_factory()
    first.storage_quota_bytes = 1_000
    second.storage_quota_bytes = 1_000
    await session.commit()

    outcomes = await asyncio.gather(
        _store(first.id, first_library.id, 800),
        _store(second.id, second_library.id, 800),
    )
    assert outcomes == ["stored", "stored"]


async def test_an_unlimited_account_still_takes_the_lock(session, signed_in):
    """`quota_bytes is None` returns early, but only *after* the lock — so an
    unlimited account cannot slip a file in between a limited one's check and
    its insert. Asserted through behaviour: it stores, and it does not hang."""
    user, library = await signed_in()
    user.storage_quota_bytes = None
    await session.commit()

    outcomes = await asyncio.wait_for(
        asyncio.gather(
            _store(user.id, library.id, 5_000_000),
            _store(user.id, library.id, 5_000_000),
        ),
        timeout=20,
    )
    assert outcomes == ["stored", "stored"]
