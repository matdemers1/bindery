"""Incremental blob replication (T-13.4, REQ-161, ADR-010).

Two properties carry this feature, and neither is obvious from the happy path.

**It must not re-upload.** Versioning is on and the credential cannot delete, so
a needless second put is not wasted bandwidth — it creates an object version
that nothing in the system is able to remove, ever.

**The ledger must not be believed.** It exists so a nightly run can skip 495
blobs without 495 round trips, and it cannot know the bucket was emptied by
hand, that a lifecycle rule matched more than intended, or that the database
was restored from an older snapshot. The bucket is the truth; the reconcile is
what compares them.
"""

import hashlib
import io

import pytest
import sqlalchemy as sa

from api import offsite
from api.db.models import OffsiteObject

CONFIG = offsite.Config(
    access_key_id="AKIAIOSFODNN7EXAMPLE",
    secret_access_key="secret",
    bucket="bindery-offsite-d3cloud",
    region="us-east-1",
    kms_key_id="alias/bindery-offsite",
)


class FakeS3:
    """Records puts and serves a list_objects_v2 paginator over what it holds."""

    def __init__(self, *, fail_on: set[str] | None = None):
        self.objects: dict[str, bytes] = {}
        self.put_calls: list[dict] = []
        self.fail_on = fail_on or set()

    def put_object(self, **kwargs):
        body = kwargs["Body"]
        data = body.read() if hasattr(body, "read") else body
        digest = hashlib.sha256(data).hexdigest()
        if digest in self.fail_on:
            raise RuntimeError("simulated transfer failure")
        # S3 rejects a mismatched ChecksumSHA256 with BadDigest; the fake holds
        # the real service to the same promise so a wrong digest cannot pass.
        expected = offsite._checksum_header(digest)
        if kwargs.get("ChecksumSHA256") != expected:
            raise AssertionError("ChecksumSHA256 did not match the body")
        self.put_calls.append(kwargs)
        self.objects[kwargs["Key"]] = data
        return {}

    def get_object(self, **kwargs):
        """Reads back what was put, and 404s the way S3 does for what was not.

        A fake that returned None for a missing key would let a drill "pass"
        against a bucket that has lost an object.
        """
        from botocore.exceptions import ClientError

        key = kwargs["Key"]
        if key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "does not exist"}},
                "GetObject",
            )
        return {"Body": io.BytesIO(self.objects[key])}

    def head_object(self, **kwargs):
        # S3 omits ChecksumSHA256 unless ChecksumMode is ENABLED. The fake
        # enforces that, because the real service's silence is the trap.
        data = self.objects[kwargs["Key"]]
        head = {"ContentLength": len(data)}
        if kwargs.get("ChecksumMode") == "ENABLED":
            head["ChecksumSHA256"] = offsite._checksum_header(
                hashlib.sha256(data).hexdigest()
            )
        return head

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        keys = sorted(self.objects)

        class Paginator:
            def paginate(self_inner, Bucket, Prefix=""):  # boto3 spells these capitalised
                matching = [k for k in keys if k.startswith(Prefix)]
                # Two pages, so a bug that only reads the first is caught.
                half = max(1, len(matching) // 2)
                for chunk in (matching[:half], matching[half:]):
                    yield {"Contents": [{"Key": k} for k in chunk]} if chunk else {}

        return Paginator()

    def delete_object(self, **kwargs):  # pragma: no cover
        raise AssertionError("the offsite credential must never delete")


@pytest.fixture(autouse=True)
async def empty_ledger(session):
    """A clean ledger per test.

    `tmp_path` is per-test but the fixture blobs are not: the same five strings
    hash to the same five addresses every time, which is the whole point of
    content addressing. The database migrates once per session, so without this
    the second test in the file sees the first test's uploads and skips
    everything — and every assertion about "uploaded 5" silently becomes an
    assertion about test ordering.
    """
    await session.execute(sa.delete(OffsiteObject))
    await session.commit()


@pytest.fixture
def blob_root(tmp_path):
    """Five blobs, laid out the way the archive really lays them out."""
    root = tmp_path / "blobs"
    for index in range(5):
        data = f"blob number {index}".encode()
        digest = hashlib.sha256(data).hexdigest()
        path = root / digest[:2] / digest[2:4] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def test_the_object_key_mirrors_the_on_disk_layout():
    """`api/storage/blobs.py` fans out two levels, not one.

    This is not cosmetic. The local path and the object key have to be
    derivable from each other in both directions, or a restore cannot find the
    blob a dump refers to.
    """
    from api.storage.blobs import blob_path

    digest = hashlib.sha256(b"anything").hexdigest()
    key = offsite.object_key_for_blob(digest)
    assert key == f"blobs/{digest[:2]}/{digest[2:4]}/{digest}"
    # The tail of the real on-disk path is exactly the tail of the object key.
    assert str(blob_path(digest)).endswith(key[len("blobs/"):])
    assert offsite.sha256_from_object_key(key) == digest


async def test_a_first_run_uploads_everything(session, blob_root):
    fake = FakeS3()
    result = await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)
    assert result.uploaded == 5
    assert result.skipped == 0
    assert result.ok
    assert len(fake.objects) == 5


async def test_a_second_run_uploads_nothing(session, blob_root):
    """The property the whole ledger exists for.

    With versioning on and no delete permission, a re-upload leaves a version
    nothing can clean up — so "skipped" here is a correctness result, not a
    performance one.
    """
    fake = FakeS3()
    await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)
    before = len(fake.put_calls)

    again = await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)
    assert again.uploaded == 0
    assert again.skipped == 5
    assert len(fake.put_calls) == before, "a second run put objects it already had"


async def test_the_upload_hands_s3_the_hash_to_check_against(session, blob_root):
    """The archive already knows every blob's hash, so the transfer is checked.

    A truncated or corrupted body is refused with BadDigest rather than stored.
    ETag cannot serve this purpose: under SSE-KMS it is not the MD5 of the
    object, which was measured against the real bucket rather than assumed.
    """
    fake = FakeS3()
    await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)
    for call in fake.put_calls:
        assert call["ChecksumAlgorithm"] == "SHA256"
        assert call["ServerSideEncryption"] == "aws:kms"
        assert call["SSEKMSKeyId"] == CONFIG.kms_key_id
        digest = call["Key"].rsplit("/", 1)[-1]
        assert call["ChecksumSHA256"] == offsite._checksum_header(digest)


async def test_one_bad_blob_does_not_strand_the_other_four(session, blob_root):
    """A single unreadable file must not keep 495 others out of the bucket."""
    doomed = hashlib.sha256(b"blob number 2").hexdigest()
    fake = FakeS3(fail_on={doomed})

    result = await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)
    assert result.uploaded == 4
    assert not result.ok
    # The reason, never the content address: this list reaches a screen every
    # household member can open, and a hash there is an existence oracle.
    assert result.failures and all(doomed[:12] not in f for f in result.failures)

    # And the next run retries only the one that failed.
    fake.fail_on = set()
    retry = await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)
    assert retry.uploaded == 1
    assert retry.skipped == 4
    assert retry.ok


async def test_an_interrupted_run_resumes_where_it_stopped(session, blob_root):
    """Each object is committed as it lands, so a kill at object 300 resumes at
    300 rather than re-shipping 299 objects that cannot be cleaned up."""
    doomed = hashlib.sha256(b"blob number 3").hexdigest()
    fake = FakeS3(fail_on={doomed})
    first = await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)

    recorded = (await session.execute(sa.select(sa.func.count(OffsiteObject.id)))).scalar_one()
    assert recorded == first.uploaded, "the ledger and the bucket disagree after a partial run"


async def test_files_that_are_not_content_addresses_are_skipped(session, blob_root):
    """Something that wandered into the directory is not a blob, and must not
    be uploaded under a key nothing can interpret."""
    (blob_root / "ab" / "cd").mkdir(parents=True, exist_ok=True)
    (blob_root / "ab" / "cd" / "notes.txt").write_text("not a blob")

    fake = FakeS3()
    result = await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)
    assert result.uploaded == 5
    assert not any(call["Key"].endswith("notes.txt") for call in fake.put_calls)


async def test_reconcile_confirms_what_is_really_there(session, blob_root):
    fake = FakeS3()
    await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)

    result = await offsite.reconcile(session, CONFIG, fake)
    assert result.in_bucket == 5
    assert result.confirmed == 5
    assert result.missing_from_bucket == 0

    rows = (await session.execute(sa.select(OffsiteObject))).scalars().all()
    assert all(row.verified_at is not None for row in rows)


async def test_reconcile_catches_a_bucket_emptied_behind_our_back(session, blob_root):
    """The failure the ledger is structurally incapable of noticing.

    Nothing is deleted from the ledger — REQ-090 — but `verified_at` is cleared,
    and that is what makes the next sync ship them again.
    """
    fake = FakeS3()
    await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)
    fake.objects.clear()

    result = await offsite.reconcile(session, CONFIG, fake)
    assert result.in_bucket == 0
    assert result.missing_from_bucket == 5

    rows = (await session.execute(sa.select(OffsiteObject))).scalars().all()
    assert len(rows) == 5, "reconcile must not delete ledger rows"
    assert all(row.verified_at is None for row in rows)


async def test_reconcile_adopts_objects_the_ledger_never_knew_about(session, blob_root):
    """A database restored from an older snapshot, or a run that died between
    the put and the commit. Recording them prevents a pointless re-upload."""
    fake = FakeS3()
    await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)
    await session.execute(sa.delete(OffsiteObject))
    await session.commit()

    result = await offsite.reconcile(session, CONFIG, fake)
    assert result.unrecorded == 5

    again = await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)
    assert again.uploaded == 0, "adopted objects were re-uploaded anyway"
    assert again.skipped == 5


async def test_reconcile_reads_every_page(session, blob_root):
    """The fake paginates deliberately. A reconcile that reads only the first
    page would report half the bucket missing and re-upload it."""
    fake = FakeS3()
    await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)
    result = await offsite.reconcile(session, CONFIG, fake)
    assert result.in_bucket == 5


async def test_nothing_in_the_sync_path_deletes(session, blob_root):
    """Bindery holds no delete permission by design. A cleanup step added here
    would fail in production as AccessDenied and look like a broken bucket."""
    fake = FakeS3()
    await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)
    await offsite.reconcile(session, CONFIG, fake)
    # FakeS3.delete_object raises if it is ever called.


async def test_the_stored_checksum_is_read_with_checksum_mode_enabled(session, blob_root):
    """S3 omits the checksum unless asked, and says nothing about omitting it.

    A verification pass that called `head_object` plainly would get `None` and
    could reasonably conclude no checksum was stored — then skip the check, or
    re-upload the whole archive. Verified against the live bucket: the same
    object returns `None` without the flag and the correct digest with it.
    """
    fake = FakeS3()
    await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)

    digest = hashlib.sha256(b"blob number 1").hexdigest()
    key = offsite.object_key_for_blob(digest)
    assert offsite.stored_checksum(fake, CONFIG, key) == offsite._checksum_header(digest)
    assert offsite.checksum_matches(fake, CONFIG, key, digest)
    assert not offsite.checksum_matches(fake, CONFIG, key, "0" * 64)


async def test_a_failure_reason_never_carries_a_content_address(session, blob_root):
    """Migration 0017 closed cross-tenant blob-existence leaking through dedup.
    A hash on the Trust screen would reopen it through a different door: anyone
    holding a copy of a file could confirm that someone here holds it too."""
    fake = FakeS3(fail_on={hashlib.sha256(b"blob number 0").hexdigest()})
    result = await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)

    for failure in result.failures + result.summarised():
        assert not any(len(word) >= 12 and all(c in "0123456789abcdef" for c in word)
                       for word in failure.replace(":", " ").split()), failure


async def test_identical_failures_are_summarised_with_a_count(session, blob_root):
    fake = FakeS3(fail_on={
        hashlib.sha256(f"blob number {i}".encode()).hexdigest() for i in range(3)
    })
    result = await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)
    assert result.summarised() == ["3 blobs: RuntimeError: simulated transfer failure"]


async def test_a_missing_object_is_actually_re_uploaded_by_the_next_sync(session, blob_root):
    """The claim `reconcile` makes about itself, asserted rather than believed.

    Its docstring says clearing `verified_at` "is what makes the next sync
    re-upload it". That was not true: `_already_shipped` selected every ledger
    row with a blob key regardless, so an object the reconcile had just found
    missing was skipped by the very sync that was supposed to replace it. The
    bucket stayed short, the ledger stayed confident, and nothing ever said so.
    """
    fake = FakeS3()
    await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)
    assert len(fake.objects) == 5

    # Somebody empties the bucket from the console.
    fake.objects.clear()
    result = await offsite.reconcile(session, CONFIG, fake)
    assert result.missing_from_bucket == 5

    again = await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)
    assert again.uploaded == 5, "the reconcile noticed the gap and nothing filled it"
    assert len(fake.objects) == 5


async def test_adopting_an_object_never_overwrites_a_known_size(session, blob_root):
    """`_record` became an upsert so an upload could clear an absence marker.

    Reconcile adopts objects with no size, and if it shared that upsert it would
    quietly zero the size of everything it adopted — a silent corruption of the
    ledger by the very pass that exists to repair it.
    """
    fake = FakeS3()
    await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)

    sizes_before = {
        row.object_key: row.byte_size
        for row in (await session.execute(sa.select(OffsiteObject))).scalars()
    }
    assert all(size > 0 for size in sizes_before.values())

    await offsite.reconcile(session, CONFIG, fake)

    sizes_after = {
        row.object_key: row.byte_size
        for row in (await session.execute(sa.select(OffsiteObject))).scalars()
    }
    assert sizes_after == sizes_before


async def test_an_object_that_comes_back_stops_being_absent(session, blob_root):
    """A reconcile that finds it again clears the marker, so it is not shipped
    a second time for no reason."""
    fake = FakeS3()
    await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)
    held = dict(fake.objects)

    fake.objects.clear()
    await offsite.reconcile(session, CONFIG, fake)
    fake.objects.update(held)
    await offsite.reconcile(session, CONFIG, fake)

    again = await offsite.sync_blobs(session, CONFIG, fake, blob_root=blob_root)
    assert again.uploaded == 0
    assert again.skipped == 5
