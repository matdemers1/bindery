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
import logging
import secrets
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from api import settings_store

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
    if configured and kms_arn and configured not in kms_arn and not config.kms_key_id.startswith("alias/"):
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
