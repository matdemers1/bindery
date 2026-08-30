"""Storage quotas (T-10.15, REQ-141).

The Zima has one disk pool and is about to hold other households' archives. When
one fills it the failure is not "that account cannot upload" — it is that OCR,
backups and Postgres stop, for everyone.
"""

import uuid

import pytest

from api import quota
from api.db.enums import IngestSource, LibraryKind, MembershipRole
from api.db.models import Library, Membership, SourceFile


async def _file(session, library, size: int, sha: str | None = None) -> SourceFile:
    source_file = SourceFile(
        library_id=library.id,
        sha256=sha or (uuid.uuid4().hex * 2)[:64],
        byte_size=size,
        original_filename="scan.pdf",
        ingest_source=IngestSource.WEB_UPLOAD,
    )
    session.add(source_file)
    await session.flush()
    return source_file


async def test_one_library_cannot_hold_the_same_bytes_twice(
    session, signed_in
) -> None:
    """Deduplication inside a library is the database's job, not the sum's.

    `uq_source_file_library_sha256` makes a second row impossible, so usage
    never has to reason about it — which is why the interesting case is the one
    below, across an account's own libraries.
    """
    import sqlalchemy.exc

    _user, library = await signed_in()
    sha = (uuid.uuid4().hex * 2)[:64]
    await _file(session, library, 1_000, sha=sha)

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await _file(session, library, 1_000, sha=sha)
    await session.rollback()


async def test_an_upload_past_the_limit_is_refused_with_the_numbers(
    session, signed_in
) -> None:
    """"Quota exceeded" with no figures is a support conversation."""
    user, library = await signed_in()
    user.storage_quota_bytes = 2_000
    await _file(session, library, 1_800)
    await session.commit()

    with pytest.raises(quota.QuotaExceeded) as raised:
        await quota.check(session, user, 500)

    message = str(raised.value)
    assert "1.8 KB" in message and "2.0 KB" in message


async def test_no_quota_means_no_limit(session, signed_in) -> None:
    user, library = await signed_in()
    user.storage_quota_bytes = None
    await _file(session, library, 10_000_000)
    await session.commit()

    usage = await quota.check(session, user, 10_000_000_000)
    assert usage.unlimited


async def test_usage_only_counts_this_account_s_libraries(
    session, signed_in, user_factory
) -> None:
    """The bug the first version had: a correlated LATERAL that SQLAlchemy
    turned into a cartesian product, so every account was reported as holding
    the entire archive."""
    user, library = await signed_in()
    await _file(session, library, 1_000)

    _other_user, other_library = await user_factory()
    await _file(session, other_library, 9_999_999)
    await session.commit()

    usage = await quota.usage_for(session, user)
    assert usage.used_bytes == 1_000, "somebody else's files are not yours"

    by_account = {row["email"]: row for row in await quota.usage_by_account(session)}
    assert by_account[user.email]["used_bytes"] == 1_000
    assert by_account[user.email]["files"] == 1


async def test_an_account_holding_nothing_reports_zero(session, user_factory) -> None:
    """An outer join, not an inner one — a new account must appear in the panel."""
    user, _ = await user_factory()
    await session.commit()

    by_account = {row["email"]: row for row in await quota.usage_by_account(session)}
    assert by_account[user.email]["used_bytes"] == 0


async def test_two_libraries_sharing_a_blob_charge_once(
    session, signed_in
) -> None:
    user, library = await signed_in()
    second = Library(name="Second", kind=LibraryKind.PERSONAL)
    session.add(second)
    await session.flush()
    session.add(
        Membership(user_id=user.id, library_id=second.id, role=MembershipRole.OWNER)
    )
    sha = (uuid.uuid4().hex * 2)[:64]
    await _file(session, library, 4_000, sha=sha)
    await _file(session, second, 4_000, sha=sha)
    await session.commit()

    usage = await quota.usage_for(session, user)
    assert usage.used_bytes == 4_000
