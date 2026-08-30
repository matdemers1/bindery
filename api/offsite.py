"""Talking to S3 (T-13.3 onward, REQ-160, ADR-010).

This module is the whole of Bindery's relationship with AWS. It lives under
`api/` rather than `worker/` because the connection test is a synchronous
request made on a human's behalf — the same reason the Anthropic client is here
— and because `worker/` may import `api/` while the reverse is forbidden.

The design rule for everything in here: **a credential that cannot delete.**
Nothing in this module calls `delete_object`, and the IAM policy denies it
anyway. Rotation of old backups is an S3 lifecycle rule, which is why the probe
object below has an expiry rule of its own — Bindery writes it and physically
cannot clean it up.
"""

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from api import settings_store
from api.db.models import OffsiteObject
from api.version import build_of_this_process

log = logging.getLogger("bindery.offsite")

# Where the connection test writes. Covered by the `expire-connection-probes`
# lifecycle rule, because Bindery cannot delete what it writes and an untidied
# probe would otherwise sit in the bucket forever, once per press of the button.
PROBE_PREFIX = "_probe/"

# A backup that hangs is worse than one that fails: the failure is visible and
# the hang looks like work in progress. Deliberately short for the probe; the
# uploader in T-13.4 will want longer for large objects.
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class Config:
    access_key_id: str
    secret_access_key: str
    bucket: str
    region: str
    kms_key_id: str

    @property
    def complete(self) -> bool:
        return all(
            (self.access_key_id, self.secret_access_key, self.bucket,
             self.region, self.kms_key_id)
        )

    def missing(self) -> list[str]:
        names = {
            "access key id": self.access_key_id,
            "secret access key": self.secret_access_key,
            "bucket": self.bucket,
            "region": self.region,
            "KMS key id": self.kms_key_id,
        }
        return [name for name, value in names.items() if not value]


async def config_from_settings(session: AsyncSession) -> Config:
    async def read(key: str) -> str:
        return await settings_store.get(session, key) or ""

    return Config(
        access_key_id=await read(settings_store.AWS_ACCESS_KEY_ID),
        secret_access_key=await read(settings_store.AWS_SECRET_ACCESS_KEY),
        bucket=await read(settings_store.OFFSITE_BUCKET),
        region=await read(settings_store.OFFSITE_REGION),
        kms_key_id=await read(settings_store.OFFSITE_KMS_KEY_ID),
    )


def make_client(config: Config):
    """A configured S3 client. Separated so tests can substitute one."""
    import boto3
    from botocore.config import Config as BotoConfig

    return boto3.client(
        "s3",
        region_name=config.region,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        config=BotoConfig(
            connect_timeout=CONNECT_TIMEOUT,
            read_timeout=READ_TIMEOUT,
            retries={"max_attempts": MAX_ATTEMPTS, "mode": "standard"},
        ),
    )


@dataclass
class ProbeResult:
    ok: bool
    detail: str
    # What the object actually came back encrypted under, which is the point of
    # the exercise — not what we asked for.
    encryption: str | None = None
    kms_key_arn: str | None = None
    bucket_key_enabled: bool | None = None
    checks: list[str] = field(default_factory=list)


def _explain(error: Exception, config: Config) -> str:
    """Turn a botocore error into the sentence that identifies the fix.

    Every one of these has been mistaken for a different problem at least once:
    a wrong region reads as a missing bucket, a missing KMS grant reads as a
    permissions problem with S3, and an expired credential reads as a typo.
    """
    code = getattr(error, "response", {}).get("Error", {}).get("Code", "")
    known = {
        "NoSuchBucket": (
            f"No bucket named {config.bucket!r} in {config.region}. Check the "
            "name, and check the region — a bucket in another region reports "
            "as missing rather than as misplaced."
        ),
        "InvalidAccessKeyId": "That access key id does not exist in this AWS account.",
        "SignatureDoesNotMatch": (
            "The secret access key does not match that key id. Re-paste the "
            "secret; it is shown only once when created."
        ),
        "ExpiredToken": "That credential has expired — it is a session token, not a long-term key.",
        "AccessDenied": (
            "Denied. The credential can reach AWS but is not allowed this "
            "action on this bucket — check the policy grants s3:PutObject and "
            "s3:GetObject on the bucket's objects, and kms:GenerateDataKey on "
            "the key."
        ),
        "KMS.NotFoundException": f"No KMS key matching {config.kms_key_id!r} in {config.region}.",
        "KMS.DisabledException": (
            "That KMS key is disabled. Nothing can be written or read until it "
            "is re-enabled — which is the stop-button working as designed."
        ),
        "KMSKeyDisabled": "That KMS key is disabled.",
        "KMS.KeyUnavailableException": "That KMS key exists but is not currently usable.",
    }
    if code in known:
        return known[code]
    return f"{code or type(error).__name__}: {str(error)[:300]}"


def _probe(config: Config, client) -> ProbeResult:
    """Write an object, read it back, and check what actually happened.

    A `ListBucket` would prove the credential works and prove nothing about
    whether a backup would survive. The round trip is the point: it exercises
    `PutObject`, the KMS grant that encrypts it, `GetObject`, and the KMS grant
    that decrypts it — four separate permissions that fail independently.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    # Random, so a stale read or a cached response cannot pass for a fresh one.
    nonce = secrets.token_hex(16)
    body = f"bindery offsite probe {nonce}\n".encode()
    key = f"{PROBE_PREFIX}connection-test"
    checks: list[str] = []

    try:
        # The KMS key is named explicitly rather than relying on the bucket's
        # default. If the configured key id is wrong, this must fail — a put
        # that silently falls back to the bucket default would report success
        # for a key that is not the one being configured, which is precisely
        # the acceptance criterion for REQ-160.
        put = client.put_object(
            Bucket=config.bucket,
            Key=key,
            Body=body,
            ServerSideEncryption="aws:kms",
            SSEKMSKeyId=config.kms_key_id,
        )
        checks.append("wrote an object")

        got = client.get_object(Bucket=config.bucket, Key=key)
        returned = got["Body"].read()
        checks.append("read it back")
    except (ClientError, BotoCoreError) as error:
        return ProbeResult(ok=False, detail=_explain(error, config), checks=checks)

    if returned != body:
        # Never yet observed, and the one failure that would make every other
        # check meaningless: a backup that restores to something else.
        return ProbeResult(
            ok=False,
            detail="The object read back did not match the bytes written.",
            checks=checks,
        )
    checks.append("bytes matched")

    encryption = got.get("ServerSideEncryption")
    kms_arn = got.get("SSEKMSKeyId")
    bucket_key = got.get("BucketKeyEnabled")

    if encryption != "aws:kms":
        return ProbeResult(
            ok=False,
            detail=(
                f"The object was stored with {encryption or 'no'} encryption, not "
                "aws:kms. It would be readable without your key."
            ),
            encryption=encryption, kms_key_arn=kms_arn,
            bucket_key_enabled=bucket_key, checks=checks,
        )
    checks.append("encrypted with aws:kms")

    # The setting may hold a full ARN, a bare UUID or an alias. For the first
    # two, the response's ARN ends in the UUID and can be compared directly.
    #
    # An alias is skipped, and that is correct rather than a shortcut: S3
    # resolves `alias/bindery-offsite` and reports the *key* ARN back, so there
    # is nothing to compare it against — and the alias is itself the
    # configuration, so whatever it points at is by definition the configured
    # key. Verified against the real bucket on 2026-08-30: an alias put returns
    # the resolved key ARN, and a non-existent alias fails the put outright with
    # KMS.NotFoundException. So a typo in an alias cannot slip through here.
    configured = config.kms_key_id.rsplit("/", 1)[-1].strip()
    is_alias = config.kms_key_id.startswith("alias/")
    if configured and kms_arn and not is_alias and configured not in kms_arn:
        return ProbeResult(
            ok=False,
            detail=(
                f"Stored under {kms_arn}, which is not the key you configured "
                f"({config.kms_key_id})."
            ),
            encryption=encryption, kms_key_arn=kms_arn,
            bucket_key_enabled=bucket_key, checks=checks,
        )
    checks.append("under the configured key")

    if not bucket_key:
        # Not a failure — it works — but it is a standing cost multiplier worth
        # saying out loud, because nothing else will ever mention it.
        checks.append("note: S3 Bucket Keys are off, so every object is a separate KMS request")

    put_arn = put.get("SSEKMSKeyId") or kms_arn
    return ProbeResult(
        ok=True,
        detail=f"Wrote and read back {len(body)} bytes, encrypted with {put_arn}.",
        encryption=encryption,
        kms_key_arn=kms_arn,
        bucket_key_enabled=bool(bucket_key),
        checks=checks,
    )


async def probe(config: Config, *, client_factory=make_client) -> ProbeResult:
    """`_probe` off the event loop — boto3 is synchronous and this does real I/O."""
    if not config.complete:
        return ProbeResult(
            ok=False,
            detail="Not configured yet — missing the " + ", ".join(config.missing()) + ".",
        )
    try:
        client = client_factory(config)
    except Exception as error:  # a malformed region reaches boto3 as a ValueError
        return ProbeResult(ok=False, detail=f"{type(error).__name__}: {str(error)[:200]}")
    return await asyncio.to_thread(_probe, config, client)


# ---------------------------------------------------------------------------
# Blob replication (T-13.4, REQ-161)
# ---------------------------------------------------------------------------

BLOB_PREFIX = "blobs/"

# S3 accepts a single PUT up to 5 GB. The largest blob in the real archive is
# 29 MB, so multipart is not needed and is not written — but a file that would
# silently fail at the API boundary should say so here instead, in a sentence
# that names multipart as the fix.
MAX_SINGLE_PUT = 4_500_000_000


def object_key_for_blob(sha256: str) -> str:
    """Mirror the on-disk layout exactly.

    `api/storage/blobs.py` fans out two levels — `<aa>/<bb>/<sha>` — and not
    one. Getting this wrong is not a cosmetic difference: the local path and
    the object key have to be derivable from each other in both directions, or
    a restore cannot find the blob a dump refers to.
    """
    return f"{BLOB_PREFIX}{sha256[:2]}/{sha256[2:4]}/{sha256}"


def sha256_from_object_key(key: str) -> str | None:
    """The inverse, used by the reconcile to read the bucket back."""
    if not key.startswith(BLOB_PREFIX):
        return None
    tail = key[len(BLOB_PREFIX):].split("/")
    if len(tail) != 3 or len(tail[2]) != 64:
        return None
    return tail[2]


def _checksum_header(sha256_hex: str) -> str:
    """S3 wants the digest base64-encoded, not hex."""
    return base64.b64encode(bytes.fromhex(sha256_hex)).decode()


def upload_blob(client, config: Config, path: Path, sha256: str) -> int:
    """Put one blob, with S3 verifying the bytes against the hash we already have.

    `ChecksumSHA256` is the point of this function. The archive already knows
    every blob's hash, so handing it to S3 turns the upload into a checked
    transfer: a truncated or corrupted body is refused with `BadDigest` rather
    than stored. Verified against the live bucket — a deliberately wrong digest
    is rejected.

    ETag would have been the obvious way to confirm the same thing and does not
    work: **under SSE-KMS the ETag is not the MD5 of the object.** Measured, not
    assumed. A verification built on it would never have matched.
    """
    size = path.stat().st_size
    if size > MAX_SINGLE_PUT:
        raise RuntimeError(
            f"{path.name} is {size} bytes, past the single-PUT limit. This needs "
            "a multipart upload, which is not implemented — the largest blob in "
            "the archive was 29 MB when this was written."
        )
    with path.open("rb") as handle:
        client.put_object(
            Bucket=config.bucket,
            Key=object_key_for_blob(sha256),
            Body=handle,
            ServerSideEncryption="aws:kms",
            SSEKMSKeyId=config.kms_key_id,
            ChecksumAlgorithm="SHA256",
            ChecksumSHA256=_checksum_header(sha256),
        )
    return size


def stored_checksum(client, config: Config, key: str) -> str | None:
    """The SHA-256 S3 holds for an object, base64-encoded as S3 spells it.

    Always via `ChecksumMode="ENABLED"`, and that is the entire reason this
    function exists rather than a bare `head_object` at each call site.
    **Without the flag the field is simply absent** — not an error, not a
    warning, just `None` — so a verification pass that forgot it would conclude
    no checksum was ever stored and either skip the check or re-upload the
    archive. Measured against the live bucket: same object, `None` without the
    flag and the correct digest with it.
    """
    head = client.head_object(Bucket=config.bucket, Key=key, ChecksumMode="ENABLED")
    return head.get("ChecksumSHA256")


def checksum_matches(client, config: Config, key: str, sha256_hex: str) -> bool:
    """Does the object S3 holds have the hash the archive says it should."""
    return stored_checksum(client, config, key) == _checksum_header(sha256_hex)


async def _already_shipped(session: AsyncSession) -> set[str]:
    """Keys the ledger records **and** does not know to be missing.

    The `absent_at` half is load-bearing. Without it a reconcile could find an
    object gone from the bucket, say so, and the next sync would skip it anyway
    — which is what this did until the offsite drill made it worth checking.
    """
    rows = await session.execute(
        sa.select(OffsiteObject.object_key).where(
            OffsiteObject.object_key.startswith(BLOB_PREFIX),
            OffsiteObject.absent_at.is_(None),
        )
    )
    return set(rows.scalars().all())


async def _record(session: AsyncSession, key: str, sha256: str | None, size: int) -> None:
    """Record an object this process just uploaded.

    Idempotent, because the upload it records is idempotent: a run killed
    between the put and the commit re-uploads on the next pass and arrives here
    twice. On conflict it refreshes rather than doing nothing, because the row
    may be one a reconcile marked absent — and an upload is the most direct
    possible evidence that it is not absent any more.
    """
    now = datetime.now(UTC)
    await session.execute(
        pg_insert(OffsiteObject)
        .values(object_key=key, sha256=sha256, byte_size=size, uploaded_at=now)
        .on_conflict_do_update(
            index_elements=[OffsiteObject.object_key],
            set_={"uploaded_at": now, "byte_size": size, "absent_at": None},
        )
    )


async def _adopt(session: AsyncSession, key: str, sha256: str | None) -> None:
    """Record an object found in the bucket that the ledger did not know about.

    Separate from `_record` and deliberately `do_nothing`: the size is unknown
    here, and this must never overwrite a real one with a placeholder. That is
    not hypothetical — `_record` became an upsert to clear `absent_at`, and
    reconcile calling it with `byte_size=0` would have quietly zeroed the size
    of every object it adopted.
    """
    await session.execute(
        pg_insert(OffsiteObject)
        .values(object_key=key, sha256=sha256, byte_size=0, uploaded_at=datetime.now(UTC))
        .on_conflict_do_nothing(index_elements=[OffsiteObject.object_key])
    )


@dataclass
class SyncResult:
    uploaded: int = 0
    skipped: int = 0
    bytes_sent: int = 0
    # Reasons, deliberately **without** the blob hash. The hash goes to the log,
    # which is library-scoped and redacted (migration 0018); this list is shown
    # on a screen every household member can open. A content address is a
    # existence oracle — "does anyone here hold this exact file?" — which is the
    # cross-tenant leak migration 0017 was written to close, and putting hashes
    # back on a shared screen would reopen it through a different door.
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def summarised(self) -> list[str]:
        """Distinct reasons with counts.

        Twelve identical "Denied." lines say the same thing once; "12 blobs:
        Denied." says it in a way that names the scale of the problem.
        """
        counts: dict[str, int] = {}
        for reason in self.failures:
            counts[reason] = counts.get(reason, 0) + 1
        return [
            reason if count == 1 else f"{count} blobs: {reason}"
            for reason, count in counts.items()
        ]


def local_blobs(blob_root: Path) -> list[tuple[str, Path]]:
    """Every blob on disk, as (sha256, path).

    A file whose name is not a 64-character hex digest is not a blob — it is
    something that wandered into the directory — and is skipped rather than
    uploaded under a key nothing can interpret.
    """
    found = []
    for path in sorted(blob_root.rglob("*")):
        if not path.is_file():
            continue
        name = path.name
        if len(name) != 64:
            log.warning("skipping %s: not a content address", path)
            continue
        try:
            int(name, 16)
        except ValueError:
            log.warning("skipping %s: not a content address", path)
            continue
        found.append((name, path))
    return found


async def sync_blobs(
    session: AsyncSession,
    config: Config,
    client,
    *,
    blob_root: Path,
    commit: bool = True,
) -> SyncResult:
    """Upload every blob the ledger does not already account for.

    Each object is recorded and committed as it lands, so a run interrupted at
    object 300 resumes at 300 rather than starting again. That matters more
    than it sounds: with versioning on and no delete permission, re-uploading
    299 unchanged objects would leave 299 versions nothing can remove.

    One failure does not stop the run. A single unreadable file should not
    prevent the other 495 blobs from reaching the bucket — the failures are
    collected and reported, and the next run retries them.
    """
    result = SyncResult()
    shipped = await _already_shipped(session)

    for sha256, path in local_blobs(blob_root):
        key = object_key_for_blob(sha256)
        if key in shipped:
            result.skipped += 1
            continue
        try:
            size = upload_blob(client, config, path, sha256)
        except Exception as error:  # collected and reported, never swallowed
            # The hash goes here, to the log, and not into `failures` — see the
            # note on SyncResult.
            log.error("offsite upload failed for %s: %s", sha256[:12], error)
            result.failures.append(_explain(error, config))
            continue
        await _record(session, key, sha256, size)
        if commit:
            await session.commit()
        result.uploaded += 1
        result.bytes_sent += size

    if commit:
        await session.commit()
    return result


@dataclass
class ReconcileResult:
    in_bucket: int = 0
    confirmed: int = 0
    missing_from_bucket: int = 0
    unrecorded: int = 0


async def reconcile(
    session: AsyncSession, config: Config, client, *, commit: bool = True
) -> ReconcileResult:
    """Compare the ledger against the bucket, and believe the bucket.

    The ledger is an optimisation and optimisations can be wrong: an object
    removed from the console, a lifecycle rule that matched more than intended,
    a restore of the database to an older snapshot. None of those are visible
    from the ledger, which will happily report everything shipped.

    A row whose object is gone has `verified_at` cleared rather than being
    deleted, so the next sync re-uploads it. Nothing here removes a row —
    nothing in Bindery removes rows on its own (REQ-090), and a ledger that
    forgets is a ledger that cannot be audited.
    """
    result = ReconcileResult()
    seen: set[str] = set()

    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=config.bucket, Prefix=BLOB_PREFIX):
        for entry in page.get("Contents", []):
            seen.add(entry["Key"])
    result.in_bucket = len(seen)

    rows = (
        await session.execute(
            sa.select(OffsiteObject).where(
                OffsiteObject.object_key.startswith(BLOB_PREFIX)
            )
        )
    ).scalars().all()

    now = datetime.now(UTC)
    recorded = set()
    for row in rows:
        recorded.add(row.object_key)
        if row.object_key in seen:
            row.verified_at = now
            # It is back, or it never went. Either way this row no longer
            # describes an absence.
            row.absent_at = None
            result.confirmed += 1
        else:
            # It was shipped once and is not there now. Marking it absent is
            # what makes the next sync ship it again — and it has to be its own
            # column, because `verified_at IS NULL` also means "uploaded a
            # moment ago and never reconciled".
            row.verified_at = None
            row.absent_at = now
            result.missing_from_bucket += 1
            log.warning(
                "offsite object recorded but absent from bucket: %s", row.object_key
            )

    # Objects in the bucket the ledger never knew about — a database restored
    # from an older snapshot, or a run that died between the put and the
    # commit. Recording them prevents a pointless re-upload.
    for key in seen - recorded:
        sha = sha256_from_object_key(key)
        await _adopt(session, key, sha)
        result.unrecorded += 1

    if commit:
        await session.commit()
    if result.missing_from_bucket:
        log.error(
            "%s objects the ledger claimed are not in the bucket — they will be "
            "re-uploaded on the next sync", result.missing_from_bucket,
        )
    return result


# ---------------------------------------------------------------------------
# The database dump, and the manifest that makes a restore checkable
# (T-13.5, REQ-162)
# ---------------------------------------------------------------------------


class Kind(StrEnum):
    """Which retention the run is writing into.

    Two schedules rather than one because the storage cost is indistinguishable
    — twelve dumps is about 52 MB — and the failure they guard against is
    different. Daily catches "I broke something yesterday". Weekly catches "the
    taxonomy merge three weeks ago was wrong", which is the one you notice late.
    """

    DAILY = "daily"
    WEEKLY = "weekly"


DUMP_PREFIX = "dumps/"
MANIFEST_PREFIX = "manifests/"


def dump_object_key(kind: Kind, stamp: datetime) -> str:
    """`dumps/daily/<ISO>.dump`, `dumps/weekly/<ISO week>.dump`.

    The weekly key is the ISO week rather than a timestamp, so a re-run in the
    same week overwrites its own generation instead of consuming one of the
    five the retention window holds.
    """
    if kind is Kind.WEEKLY:
        year, week, _ = stamp.isocalendar()
        return f"{DUMP_PREFIX}weekly/{year}-W{week:02d}.dump"
    return f"{DUMP_PREFIX}daily/{stamp.strftime('%Y-%m-%dT%H%M%SZ')}.dump"


def manifest_object_key(dump_key: str) -> str:
    return MANIFEST_PREFIX + dump_key[len(DUMP_PREFIX):].removesuffix(".dump") + ".json"


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload_bytes(client, config: Config, key: str, payload: bytes) -> None:
    """A small object whose hash we compute here rather than already knowing."""
    digest = hashlib.sha256(payload).hexdigest()
    client.put_object(
        Bucket=config.bucket,
        Key=key,
        Body=payload,
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=config.kms_key_id,
        ChecksumAlgorithm="SHA256",
        ChecksumSHA256=_checksum_header(digest),
    )


def upload_dump(client, config: Config, path: Path, key: str) -> tuple[str, int]:
    """Put the dump under a key the retention rules will eventually expire."""
    digest = _sha256_of(path)
    size = path.stat().st_size
    if size > MAX_SINGLE_PUT:
        raise RuntimeError(
            f"the dump is {size} bytes, past the single-PUT limit — this needs a "
            "multipart upload, which is not implemented."
        )
    with path.open("rb") as handle:
        client.put_object(
            Bucket=config.bucket,
            Key=key,
            Body=handle,
            ServerSideEncryption="aws:kms",
            SSEKMSKeyId=config.kms_key_id,
            ChecksumAlgorithm="SHA256",
            ChecksumSHA256=_checksum_header(digest),
        )
    return digest, size


def human_bytes(count: int) -> str:
    """Sizes for a sentence someone reads, not for a log line.

    "4402188 byte dump" is technically complete and nobody can see at a glance
    that it is fine. "4.2 MB" is the same fact in a form that answers the
    question being asked.
    """
    size = float(count)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "bytes":
                return f"{int(size)} bytes"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


@dataclass
class ReplicationResult:
    kind: Kind
    ok: bool = False
    detail: str = ""
    dump_key: str | None = None
    dump_bytes: int = 0
    blobs_uploaded: int = 0
    blobs_skipped: int = 0
    bytes_sent: int = 0
    failures: list[str] = field(default_factory=list)


async def replicate(
    session: AsyncSession,
    config: Config,
    client,
    *,
    kind: Kind,
    blob_root: Path,
    integrity_report=None,
    allow_unhealthy: bool = False,
    stamp: datetime | None = None,
) -> ReplicationResult:
    """One offsite generation: dump, then blobs, then the dump goes up last.

    **The order is not the local backup's order, and the difference is the whole
    point.** Locally the rule is dump-then-copy-blobs: blobs are append-only, so
    a blob copied after the dump is an unreferenced orphan, which is harmless.

    Replication is incremental and interruptible, which adds a case the local
    copy does not have. Upload the dump first and fail partway through the
    blobs, and the *bucket* now holds a dump referencing objects that are not
    there — a dangling reference that persists until some later run finishes.
    So the dump is written to disk first, at the same instant it would have
    been, and is the last thing sent. Anything it references is already up;
    anything uploaded after it is an orphan. Safe by construction, as before.

    Integrity is checked before any of it, for the reason `run_backup` gives: a
    backup taken over a corrupt blob is a corrupt backup, faithfully replicated
    and eventually rotated into every generation you have.
    """
    from api.export import backup as local_backup

    result = ReplicationResult(kind=kind)

    if integrity_report is not None and not integrity_report.healthy and not allow_unhealthy:
        result.detail = (
            f"integrity check failed ({len(integrity_report.corrupt)} corrupt, "
            f"{len(integrity_report.missing)} missing) — refusing to replicate over it"
        )
        result.failures.append(result.detail)
        return result

    stamp = stamp or datetime.now(UTC)
    dump_key = dump_object_key(kind, stamp)

    with tempfile.TemporaryDirectory(prefix="bindery-offsite-") as staging:
        # Step 1: the dump, to disk only. Taken before the blob sync so that
        # everything it references is captured by the sync that follows.
        dump_path = Path(staging) / "bindery.dump"
        try:
            local_backup.dump_database(dump_path)
        except Exception as error:
            result.detail = f"pg_dump failed: {str(error)[:300]}"
            result.failures.append(result.detail)
            return result

        # Step 2: blobs. A superset of anything the dump can refer to, because
        # blobs are append-only and never removed.
        sync = await sync_blobs(session, config, client, blob_root=blob_root)
        result.blobs_uploaded = sync.uploaded
        result.blobs_skipped = sync.skipped
        result.bytes_sent = sync.bytes_sent
        result.failures.extend(sync.summarised())

        if sync.failures:
            # Deliberately no dump. A dump in the bucket is a promise that its
            # blobs are there too, and this run cannot make that promise.
            result.detail = (
                f"{len(sync.failures)} blob(s) failed to upload — the dump was not "
                "sent, because a dump whose blobs are missing is an unrestorable "
                "backup rather than a partial one."
            )
            return result

        # Step 3: the dump, last.
        try:
            digest, size = upload_dump(client, config, dump_path, dump_key)
        except Exception as error:
            result.detail = f"the dump failed to upload: {_explain(error, config)}"
            result.failures.append(result.detail)
            return result

    await _record(session, dump_key, digest, size)

    manifest = {
        "created_at": stamp.isoformat(),
        "kind": kind.value,
        "dump": {"key": dump_key, "bytes": size, "sha256": digest},
        "blobs": {
            "uploaded_this_run": sync.uploaded,
            "already_present": sync.skipped,
            "total_in_ledger": await _ledger_count(session),
        },
        "integrity": integrity_report.as_dict() if integrity_report else None,
        # What a restore has to match. A dump restored by code that expects a
        # different schema fails somewhere far away and much later.
        "schema_revision": await _applied_revision(session),
        "build": build_of_this_process().commit,
        "restore": "scripts/restore-drill.sh --from-s3",
        "note": (
            "Blobs were uploaded before this dump, so every blob it references "
            "is already in the bucket. Anything uploaded afterwards is an "
            "unreferenced orphan, never a dangling reference."
        ),
    }
    payload = json.dumps(manifest, indent=2).encode()
    manifest_key = manifest_object_key(dump_key)
    try:
        upload_bytes(client, config, manifest_key, payload)
    except Exception as error:
        # The dump is up and restorable; only its description is missing.
        result.failures.append(f"manifest upload failed: {_explain(error, config)}")

    await session.commit()

    result.ok = True
    result.dump_key = dump_key
    result.dump_bytes = size
    result.detail = (
        f"{human_bytes(size)} dump, {sync.uploaded} new blob(s) "
        f"({human_bytes(sync.bytes_sent)}), {sync.skipped} already present"
    )
    return result


async def _ledger_count(session: AsyncSession) -> int:
    return (
        await session.execute(
            sa.select(sa.func.count(OffsiteObject.id)).where(
                OffsiteObject.object_key.startswith(BLOB_PREFIX)
            )
        )
    ).scalar_one()


async def _applied_revision(session: AsyncSession) -> str | None:
    return (
        await session.execute(sa.text("select version_num from alembic_version"))
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# The lifecycle audit (T-13.9, REQ-166, R-21)
# ---------------------------------------------------------------------------

# A representative key of each kind the code writes, used to check the rules
# against what actually gets stored rather than against what they look like.
# Derived from the same functions that build the real keys, so a change to the
# key format cannot drift away from the rules that are supposed to match it.
def sample_keys() -> dict[str, str]:
    stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    daily = dump_object_key(Kind.DAILY, stamp)
    weekly = dump_object_key(Kind.WEEKLY, stamp)
    return {
        "blob": object_key_for_blob("a" * 64),
        "daily dump": daily,
        "weekly dump": weekly,
        "daily manifest": manifest_object_key(daily),
        "weekly manifest": manifest_object_key(weekly),
        "connection probe": f"{PROBE_PREFIX}connection-test",
    }


# What each kind of object is *supposed* to happen to. The blob entry is the
# whole point: expiring a blob is deleting the archive.
EXPECTED_EXPIRY = {
    "blob": None,
    "daily dump": "expire-daily-dumps",
    "weekly dump": "expire-weekly-dumps",
    "daily manifest": None,
    "weekly manifest": None,
    "connection probe": "expire-connection-probes",
}


def _rule_prefix(rule: dict) -> str:
    filt = rule.get("Filter") or {}
    if "Prefix" in filt:
        return filt["Prefix"] or ""
    if "And" in filt:
        return filt["And"].get("Prefix", "") or ""
    # A rule with no filter at all applies to the whole bucket.
    return rule.get("Prefix", "") or ""


def _expires_objects(rule: dict) -> bool:
    """Does this rule remove things that exist?

    `AbortIncompleteMultipartUpload` does not: it discards the fragments of an
    upload that never completed, which are not objects. It is the only rule
    allowed to apply bucket-wide, because it cannot delete an archive.

    `ExpiredObjectDeleteMarker` also removes nothing real — a delete marker with
    no versions behind it — so it is not counted either.
    """
    if rule.get("Status") != "Enabled":
        return False
    expiration = rule.get("Expiration") or {}
    if expiration.get("Days") or expiration.get("Date"):
        return True
    return bool(rule.get("NoncurrentVersionExpiration"))


def audit_lifecycle(rules: list[dict]) -> list[str]:
    """Findings, empty when the rules are safe (REQ-166).

    Two failures are being defended against, and the first is catastrophic:

    **A bucket-wide expiry deletes the archive.** Silently, with no error and no
    alert, because Bindery holds no delete permission and would neither cause it
    nor notice it. Every expiry rule must carry a prefix filter.

    **A rule that stops matching the keys the code writes.** A dump landing
    outside `dumps/daily/` is never rotated and accumulates forever; one landing
    under a prefix with a shorter retention than intended disappears early. The
    keys here come from the same functions that build the real ones, so the
    check is against what is actually stored rather than against what the rules
    look like.
    """
    findings: list[str] = []

    for rule in rules:
        if not _expires_objects(rule):
            continue
        if not _rule_prefix(rule):
            findings.append(
                f"lifecycle rule {rule.get('ID', '(unnamed)')!r} expires objects with no "
                "prefix filter — it would delete the blob pool, which is the archive"
            )

    for name, key in sample_keys().items():
        matched = [
            rule.get("ID", "(unnamed)")
            for rule in rules
            if _expires_objects(rule) and key.startswith(_rule_prefix(rule))
        ]
        expected = EXPECTED_EXPIRY[name]
        if expected is None and matched:
            findings.append(
                f"{name} objects ({key}) would be expired by {matched} — they are "
                "meant to be kept"
            )
        elif expected is not None and expected not in matched:
            findings.append(
                f"{name} objects ({key}) are not covered by {expected!r} "
                f"(matched: {matched or 'nothing'}) — they would never be rotated"
            )
    return findings


def live_lifecycle_rules(client, config: Config) -> list[dict]:
    """The rules the bucket actually has, not the ones in the repository."""
    from botocore.exceptions import ClientError

    try:
        return client.get_bucket_lifecycle_configuration(Bucket=config.bucket)["Rules"]
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code == "NoSuchLifecycleConfiguration":
            # Not an empty result. No rules means dumps accumulate forever, and
            # reporting "nothing wrong" would be the wrong answer.
            return []
        raise


# ---------------------------------------------------------------------------
# Reading the archive back (T-13.10, REQ-097)
# ---------------------------------------------------------------------------


def newest_dump(client, config: Config, kind: Kind | None = None) -> str | None:
    """The most recent dump key in the bucket, by key order.

    Both key schemes sort correctly as strings — an ISO timestamp and an ISO
    week both do — so "newest" is `max()` rather than a listing sorted by
    LastModified. That matters: `LastModified` changes if an object is ever
    rewritten, and the key is what says which generation this actually is.
    """
    prefixes = [f"{DUMP_PREFIX}{kind.value}/"] if kind else [
        f"{DUMP_PREFIX}{k.value}/" for k in Kind
    ]
    newest: str | None = None
    newest_stamp = None
    paginator = client.get_paginator("list_objects_v2")
    for prefix in prefixes:
        for page in paginator.paginate(Bucket=config.bucket, Prefix=prefix):
            for entry in page.get("Contents", []):
                if not entry["Key"].endswith(".dump"):
                    continue
                # Across the two prefixes the key formats differ, so the only
                # thing comparable between a daily and a weekly generation is
                # when it was written.
                if newest_stamp is None or entry["LastModified"] > newest_stamp:
                    newest, newest_stamp = entry["Key"], entry["LastModified"]
    return newest


def download(client, config: Config, key: str, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    body = client.get_object(Bucket=config.bucket, Key=key)["Body"].read()
    destination.write_bytes(body)
    return len(body)


@dataclass
class FetchResult:
    fetched: int = 0
    bytes_read: int = 0
    missing: list[str] = field(default_factory=list)
    corrupt: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.corrupt


def fetch_blobs(client, config: Config, shas: list[str], into: Path) -> FetchResult:
    """Pull the named originals out of the bucket and check them as they arrive.

    The local drill asks whether a file is *present* in the backup directory.
    This asks something stronger, and the offsite copy makes it possible: it
    re-hashes every object it downloads and compares against the content
    address the restored database asked for.

    A blob that is present but wrong is the failure a presence check cannot see,
    and it is the one that matters — a backup that restores cleanly and hands
    back different bytes is worse than one that fails loudly.
    """
    from botocore.exceptions import ClientError

    result = FetchResult()
    for sha in shas:
        key = object_key_for_blob(sha)
        path = into / sha[:2] / sha[2:4] / sha
        try:
            body = client.get_object(Bucket=config.bucket, Key=key)["Body"].read()
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                result.missing.append(sha)
                continue
            raise
        actual = hashlib.sha256(body).hexdigest()
        if actual != sha:
            result.corrupt.append(sha)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        result.fetched += 1
        result.bytes_read += len(body)
    return result
