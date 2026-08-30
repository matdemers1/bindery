"""The connection test (T-13.3, REQ-160, ADR-010).

The acceptance criterion is the interesting part: *a wrong key id or a missing
`kms:GenerateDataKey` must fail the test rather than pass it.* That rules out
the obvious implementation. A `ListBucket` proves the credential can reach AWS
and proves nothing about whether a backup would survive — PutObject,
kms:GenerateDataKey, GetObject and kms:Decrypt are four separate permissions
that fail independently, and a reachability check exercises none of them.

So the probe writes an object and reads it back. These tests drive it with a
stand-in for boto3, because a suite that needs AWS credentials is a suite that
does not run.
"""

import pytest

from api import offsite

KEY_UUID = "6c1e27d6-5bee-4383-8d9b-b0b653e4ff85"
KEY_ARN = f"arn:aws:kms:us-east-1:150056528345:key/{KEY_UUID}"

CONFIG = offsite.Config(
    access_key_id="AKIAIOSFODNN7EXAMPLE",
    secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    bucket="bindery-offsite-d3cloud",
    region="us-east-1",
    kms_key_id=KEY_ARN,
)


class FakeS3:
    """Enough of the S3 client to exercise the probe's decisions.

    Records what it was asked to do, so a test can assert on the *request* as
    well as the response — the explicit `SSEKMSKeyId` on put is load-bearing and
    invisible from the result alone.
    """

    def __init__(self, *, encryption="aws:kms", kms_arn=KEY_ARN,
                 bucket_key=True, corrupt=False, raises=None):
        self.encryption = encryption
        self.kms_arn = kms_arn
        self.bucket_key = bucket_key
        self.corrupt = corrupt
        self.raises = raises
        self.stored: dict[str, bytes] = {}
        self.put_calls: list[dict] = []
        self.deleted: list[str] = []

    def put_object(self, **kwargs):
        if self.raises:
            raise self.raises
        self.put_calls.append(kwargs)
        self.stored[kwargs["Key"]] = kwargs["Body"]
        return {"SSEKMSKeyId": self.kms_arn, "ServerSideEncryption": self.encryption}

    def get_object(self, **kwargs):
        body = self.stored[kwargs["Key"]]
        if self.corrupt:
            body = body + b"tampered"

        class Body:
            def read(self_inner):
                return body

        return {
            "Body": Body(),
            "ServerSideEncryption": self.encryption,
            "SSEKMSKeyId": self.kms_arn,
            "BucketKeyEnabled": self.bucket_key,
        }

    def delete_object(self, **kwargs):  # pragma: no cover - must never be called
        self.deleted.append(kwargs["Key"])
        raise AssertionError("the offsite credential must never delete")


def client_error(code: str):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": code}}, "PutObject")


async def probe_with(fake) -> offsite.ProbeResult:
    return await offsite.probe(CONFIG, client_factory=lambda _config: fake)


async def test_a_healthy_round_trip_passes():
    fake = FakeS3()
    result = await probe_with(fake)
    assert result.ok, result.detail
    assert result.encryption == "aws:kms"
    assert result.kms_key_arn == KEY_ARN
    assert "bytes matched" in result.checks


async def test_the_put_names_the_configured_key_explicitly():
    """Not the bucket default — this is what makes a wrong key id detectable.

    If the put omitted `SSEKMSKeyId`, S3 would quietly apply the bucket's
    default key and return success. The test would then pass for a key id that
    is not the one being configured, which is the exact failure REQ-160 names.
    """
    fake = FakeS3()
    await probe_with(fake)
    assert fake.put_calls[0]["SSEKMSKeyId"] == KEY_ARN
    assert fake.put_calls[0]["ServerSideEncryption"] == "aws:kms"


async def test_a_wrong_key_id_fails_rather_than_passes():
    """The object came back under a key that is not the configured one."""
    fake = FakeS3(kms_arn="arn:aws:kms:us-east-1:150056528345:key/00000000-dead-beef-0000-000000000000")
    result = await probe_with(fake)
    assert not result.ok
    assert "not the key you configured" in result.detail


async def test_unencrypted_storage_is_a_failure_not_a_success():
    """The dangerous case: the put succeeds and the archive is readable without
    the key. Nothing else in the system would ever mention it."""
    fake = FakeS3(encryption="AES256", kms_arn=None)
    result = await probe_with(fake)
    assert not result.ok
    assert "readable without your key" in result.detail


async def test_bytes_that_come_back_different_fail():
    fake = FakeS3(corrupt=True)
    result = await probe_with(fake)
    assert not result.ok
    assert "did not match" in result.detail
    # It got far enough to write and read; the check that failed is named.
    assert "read it back" in result.checks
    assert "bytes matched" not in result.checks


async def test_bucket_keys_off_is_reported_but_not_a_failure():
    """It works. It also multiplies KMS requests by the number of objects, and
    this is the only place that would ever say so."""
    fake = FakeS3(bucket_key=False)
    result = await probe_with(fake)
    assert result.ok
    assert any("Bucket Keys are off" in check for check in result.checks)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("NoSuchBucket", "check the region"),
        ("InvalidAccessKeyId", "does not exist in this AWS account"),
        ("SignatureDoesNotMatch", "does not match that key id"),
        ("AccessDenied", "kms:GenerateDataKey"),
        ("KMS.NotFoundException", "No KMS key matching"),
        ("KMS.DisabledException", "stop-button working as designed"),
    ],
)
async def test_each_failure_names_its_own_fix(code, expected):
    """A shared "could not connect" would make all six of these look alike.

    Every one has been mistaken for a different problem: a wrong region reports
    as a missing bucket, a missing KMS grant reports as an S3 permissions
    problem, and an expired credential reports as a typo.
    """
    result = await probe_with(FakeS3(raises=client_error(code)))
    assert not result.ok
    assert expected in result.detail


async def test_an_unconfigured_probe_says_what_is_missing():
    result = await offsite.probe(
        offsite.Config(access_key_id="", secret_access_key="", bucket="",
                       region="us-east-1", kms_key_id="")
    )
    assert not result.ok
    for name in ("access key id", "secret access key", "bucket", "KMS key id"):
        assert name in result.detail
    assert "region" not in result.detail.split("missing the")[1]


async def test_the_probe_never_deletes():
    """Bindery holds no delete permission, by design (ADR-010). If this module
    ever grew a cleanup step it would fail in production as AccessDenied, and
    the probe would report a broken configuration that was fine."""
    fake = FakeS3()
    await probe_with(fake)
    assert fake.deleted == []


async def test_the_probe_writes_under_the_expiring_prefix():
    """Bindery cannot delete the probe, so the bucket's lifecycle rule has to.
    A key outside `_probe/` would accumulate one immortal object per press."""
    fake = FakeS3()
    await probe_with(fake)
    assert fake.put_calls[0]["Key"].startswith(offsite.PROBE_PREFIX)


async def test_the_endpoint_requires_an_owner(client, signed_in):
    await signed_in()
    response = await client.post("/api/settings/test-offsite")
    assert response.status_code == 200
    # Unconfigured in the test database, which is the honest answer.
    assert response.json()["ok"] is False
