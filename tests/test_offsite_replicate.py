"""One offsite generation, end to end (T-13.5, REQ-162, ADR-010).

The ordering is the substance of this file, and it is **not** the local
backup's ordering.

Locally the rule is dump, then copy blobs: blobs are append-only, so a blob
copied after the dump is an unreferenced orphan, which is harmless. Replication
adds a case the local copy does not have — it is incremental and it can be
interrupted. Send the dump first, fail partway through the blobs, and the
*bucket* holds a dump referencing objects that are not in it. That is a
dangling reference, and it persists until some later run happens to finish.

So: dump to disk, sync blobs, send the dump last.
"""

import hashlib
import json
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from api import offsite
from api.db.models import OffsiteObject
from api.export.integrity import IntegrityReport

CONFIG = offsite.Config(
    access_key_id="AKIAIOSFODNN7EXAMPLE",
    secret_access_key="secret",
    bucket="bindery-offsite-d3cloud",
    region="us-east-1",
    kms_key_id="alias/bindery-offsite",
)
STAMP = datetime(2026, 8, 30, 3, 15, 0, tzinfo=UTC)


class RecordingS3:
    """Records the order of puts, which is what these tests are about."""

    def __init__(self, *, fail_blobs: set[str] | None = None, fail_dump: bool = False):
        self.objects: dict[str, bytes] = {}
        self.order: list[str] = []
        self.fail_blobs = fail_blobs or set()
        self.fail_dump = fail_dump

    def put_object(self, **kwargs):
        key = kwargs["Key"]
        body = kwargs["Body"]
        data = body.read() if hasattr(body, "read") else body
        if key.startswith(offsite.DUMP_PREFIX) and self.fail_dump:
            raise RuntimeError("simulated dump upload failure")
        if hashlib.sha256(data).hexdigest() in self.fail_blobs:
            raise RuntimeError("simulated blob failure")
        assert kwargs["ServerSideEncryption"] == "aws:kms"
        assert kwargs["ChecksumSHA256"] == offsite._checksum_header(
            hashlib.sha256(data).hexdigest()
        )
        self.objects[key] = data
        self.order.append(key)
        return {}

    def get_paginator(self, name):
        keys = sorted(self.objects)

        class Paginator:
            def paginate(self_inner, Bucket, Prefix=""):
                yield {"Contents": [{"Key": k} for k in keys if k.startswith(Prefix)]}

        return Paginator()


@pytest.fixture(autouse=True)
async def empty_ledger(session):
    await session.execute(sa.delete(OffsiteObject))
    await session.commit()


@pytest.fixture
def blob_root(tmp_path):
    root = tmp_path / "blobs"
    for index in range(3):
        data = f"replicate blob {index}".encode()
        digest = hashlib.sha256(data).hexdigest()
        path = root / digest[:2] / digest[2:4] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


@pytest.fixture
def fake_dump(monkeypatch):
    """Stand in for pg_dump. The real one needs a live server and a binary."""

    def write(destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"PGDMP fake custom-format archive")
        return destination

    monkeypatch.setattr("api.export.backup.dump_database", write)
    return write


def healthy() -> IntegrityReport:
    return IntegrityReport(started_at=STAMP, finished_at=STAMP, checked=3, ok=3)


def corrupt() -> IntegrityReport:
    report = healthy()
    report.corrupt = [{"sha256": "abc", "reason": "hash mismatch"}]
    return report


async def test_the_dump_is_sent_after_every_blob(session, blob_root, fake_dump):
    """The ordering rule, asserted on the actual sequence of puts."""
    fake = RecordingS3()
    result = await offsite.replicate(
        session, CONFIG, fake, kind=offsite.Kind.DAILY,
        blob_root=blob_root, integrity_report=healthy(), stamp=STAMP,
    )
    assert result.ok, result.detail

    dump_index = next(i for i, k in enumerate(fake.order) if k.startswith("dumps/"))
    blob_indices = [i for i, k in enumerate(fake.order) if k.startswith("blobs/")]
    assert blob_indices, "no blobs were uploaded"
    assert max(blob_indices) < dump_index, (
        "the dump was sent before a blob it may reference — a dangling "
        "reference in the bucket"
    )


async def test_a_failed_blob_means_no_dump_at_all(session, blob_root, fake_dump):
    """A dump in the bucket is a promise that its blobs are there too.

    Sending one anyway would turn a partial run into an unrestorable backup,
    and the next run would have no way to know the promise was broken.
    """
    doomed = hashlib.sha256(b"replicate blob 1").hexdigest()
    fake = RecordingS3(fail_blobs={doomed})

    result = await offsite.replicate(
        session, CONFIG, fake, kind=offsite.Kind.DAILY,
        blob_root=blob_root, integrity_report=healthy(), stamp=STAMP,
    )
    assert not result.ok
    assert not any(k.startswith("dumps/") for k in fake.objects)
    assert "unrestorable" in result.detail
    # The blobs that did succeed stay up — they are useful to the next run.
    assert len([k for k in fake.objects if k.startswith("blobs/")]) == 2


async def test_a_failing_integrity_check_refuses_to_replicate(session, blob_root, fake_dump):
    """Same argument `run_backup` makes: a backup taken over a corrupt blob is
    a corrupt backup, faithfully replicated into every generation you keep."""
    fake = RecordingS3()
    result = await offsite.replicate(
        session, CONFIG, fake, kind=offsite.Kind.DAILY,
        blob_root=blob_root, integrity_report=corrupt(), stamp=STAMP,
    )
    assert not result.ok
    assert "refusing to replicate" in result.detail
    assert fake.objects == {}, "a refusal must not upload anything at all"


async def test_the_refusal_can_be_overridden_deliberately(session, blob_root, fake_dump):
    fake = RecordingS3()
    result = await offsite.replicate(
        session, CONFIG, fake, kind=offsite.Kind.DAILY, blob_root=blob_root,
        integrity_report=corrupt(), allow_unhealthy=True, stamp=STAMP,
    )
    assert result.ok, result.detail


def test_the_weekly_key_is_the_iso_week_not_a_timestamp():
    """So a re-run in the same week overwrites its own generation rather than
    consuming one of the five the retention window holds."""
    monday = datetime(2026, 8, 24, 3, 15, tzinfo=UTC)
    friday = datetime(2026, 8, 28, 22, 0, tzinfo=UTC)
    assert offsite.dump_object_key(offsite.Kind.WEEKLY, monday) == \
        offsite.dump_object_key(offsite.Kind.WEEKLY, friday)

    tuesday = datetime(2026, 8, 25, 3, 15, tzinfo=UTC)
    assert offsite.dump_object_key(offsite.Kind.DAILY, monday) != \
        offsite.dump_object_key(offsite.Kind.DAILY, tuesday)


def test_dumps_land_under_the_prefixes_the_lifecycle_rules_expire():
    """The retention window is an S3 lifecycle rule keyed on these prefixes.
    A dump written anywhere else would simply never be rotated."""
    daily = offsite.dump_object_key(offsite.Kind.DAILY, STAMP)
    weekly = offsite.dump_object_key(offsite.Kind.WEEKLY, STAMP)
    assert daily.startswith("dumps/daily/")
    assert weekly.startswith("dumps/weekly/")
    assert offsite.manifest_object_key(daily).startswith("manifests/daily/")
    assert offsite.manifest_object_key(weekly).startswith("manifests/weekly/")


async def test_the_manifest_records_what_a_restore_has_to_match(session, blob_root, fake_dump):
    fake = RecordingS3()
    result = await offsite.replicate(
        session, CONFIG, fake, kind=offsite.Kind.DAILY,
        blob_root=blob_root, integrity_report=healthy(), stamp=STAMP,
    )
    manifest_key = offsite.manifest_object_key(result.dump_key)
    manifest = json.loads(fake.objects[manifest_key])

    assert manifest["dump"]["key"] == result.dump_key
    assert manifest["dump"]["sha256"]
    assert manifest["blobs"]["uploaded_this_run"] == 3
    # The revision matters: a dump restored by code expecting a different
    # schema fails somewhere far away and much later.
    assert manifest["schema_revision"]
    assert manifest["integrity"]["healthy"] is True


async def test_the_dump_is_recorded_in_the_ledger(session, blob_root, fake_dump):
    fake = RecordingS3()
    result = await offsite.replicate(
        session, CONFIG, fake, kind=offsite.Kind.DAILY,
        blob_root=blob_root, integrity_report=healthy(), stamp=STAMP,
    )
    row = (
        await session.execute(
            sa.select(OffsiteObject).where(OffsiteObject.object_key == result.dump_key)
        )
    ).scalar_one()
    assert row.byte_size == result.dump_bytes


async def test_a_manifest_failure_does_not_fail_the_run(session, blob_root, fake_dump):
    """The dump is up and restorable; only its description is missing. Reporting
    the whole generation as failed would invite a re-run that re-uploads a dump
    which is already there — and cannot be deleted."""
    fake = RecordingS3()
    original = fake.put_object

    def put(**kwargs):
        if kwargs["Key"].startswith(offsite.MANIFEST_PREFIX):
            raise RuntimeError("simulated manifest failure")
        return original(**kwargs)

    fake.put_object = put
    result = await offsite.replicate(
        session, CONFIG, fake, kind=offsite.Kind.DAILY,
        blob_root=blob_root, integrity_report=healthy(), stamp=STAMP,
    )
    assert result.ok
    assert any("manifest" in failure for failure in result.failures)


async def test_a_failed_dump_upload_leaves_no_ledger_row(session, blob_root, fake_dump):
    """Otherwise the ledger claims a dump that is not there, and nothing would
    ever retry it — the object key for that day is already spoken for."""
    fake = RecordingS3(fail_dump=True)
    result = await offsite.replicate(
        session, CONFIG, fake, kind=offsite.Kind.DAILY,
        blob_root=blob_root, integrity_report=healthy(), stamp=STAMP,
    )
    assert not result.ok
    rows = (
        await session.execute(
            sa.select(OffsiteObject).where(
                OffsiteObject.object_key.startswith(offsite.DUMP_PREFIX)
            )
        )
    ).scalars().all()
    assert rows == []


def test_sizes_are_written_for_someone_reading_a_screen():
    """The Trust screen renders this string verbatim. "4402188 byte dump" is
    technically complete and nobody can tell at a glance that it is fine."""
    assert offsite.human_bytes(0) == "0 bytes"
    assert offsite.human_bytes(900) == "900 bytes"
    assert offsite.human_bytes(4_402_188) == "4.2 MB"
    assert offsite.human_bytes(333_000_000) == "317.6 MB"
    assert offsite.human_bytes(5_000_000_000) == "4.7 GB"


async def test_the_detail_line_reads_as_a_sentence(session, blob_root, fake_dump):
    fake = RecordingS3()
    result = await offsite.replicate(
        session, CONFIG, fake, kind=offsite.Kind.DAILY,
        blob_root=blob_root, integrity_report=healthy(), stamp=STAMP,
    )
    assert "byte dump" not in result.detail
    assert "bytes dump" in result.detail or "KB dump" in result.detail
