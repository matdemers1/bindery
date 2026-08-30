"""The lifecycle guard (T-13.9, REQ-166, R-21).

R-21 is the most dangerous thing available in this design, and the reason is
that **Bindery cannot see it happen.** A bucket-wide expiry rule would delete
the blob pool — the archive itself — with no error, no alert, and no way for the
application to notice, because it holds no delete permission and would neither
cause the deletion nor be told about it.

So the guard runs against the checked-in configuration, which is what gets
deployed, and separately against the live bucket when credentials exist. The
first catches the mistake at the moment it is written; the second catches
someone editing the rules in the console afterwards.

The sample keys come from the same functions that build the real ones. A test
that hard-codes `dumps/daily/...` would keep passing after the key format
changed, which is precisely the drift worth catching.
"""

import json
from pathlib import Path

import pytest

from api import offsite

DEPLOYED = Path(__file__).resolve().parent.parent / "infra" / "aws" / "lifecycle.json"


def checked_in_rules() -> list[dict]:
    return json.loads(DEPLOYED.read_text())["Rules"]


def test_the_configuration_that_gets_deployed_is_safe():
    assert offsite.audit_lifecycle(checked_in_rules()) == []


def test_a_bucket_wide_expiry_is_caught():
    """The catastrophic one. Silent, total, and invisible to the application."""
    rules = checked_in_rules() + [
        {"ID": "tidy-up", "Status": "Enabled", "Filter": {"Prefix": ""},
         "Expiration": {"Days": 30}}
    ]
    findings = offsite.audit_lifecycle(rules)
    assert any("no prefix filter" in f for f in findings)
    assert any("which is the archive" in f for f in findings)


def test_a_rule_with_no_filter_at_all_is_caught():
    """`Filter` is optional in the S3 API, and its absence means "everything"."""
    rules = checked_in_rules() + [
        {"ID": "legacy", "Status": "Enabled", "Expiration": {"Days": 30}}
    ]
    assert any("no prefix filter" in f for f in offsite.audit_lifecycle(rules))


def test_a_rule_aimed_at_the_blob_prefix_is_caught():
    """Prefixed, so the first check passes — and still deletes the archive."""
    rules = checked_in_rules() + [
        {"ID": "prune-blobs", "Status": "Enabled", "Filter": {"Prefix": "blobs/"},
         "Expiration": {"Days": 365}}
    ]
    findings = offsite.audit_lifecycle(rules)
    assert any("meant to be kept" in f and "blob" in f for f in findings)


def test_a_noncurrent_version_expiry_counts_as_expiry():
    """Versioning is on. A rule that expires only noncurrent versions still
    removes real objects — every version but the newest."""
    rules = checked_in_rules() + [
        {"ID": "prune-old-versions", "Status": "Enabled", "Filter": {"Prefix": ""},
         "NoncurrentVersionExpiration": {"NoncurrentDays": 7}}
    ]
    assert any("no prefix filter" in f for f in offsite.audit_lifecycle(rules))


def test_aborting_incomplete_uploads_is_allowed_to_be_bucket_wide():
    """It is the one unfiltered rule that is safe: it discards the fragments of
    an upload that never finished, which are not objects and cannot be an
    archive. Removing this exemption would make the safe configuration fail."""
    assert offsite.audit_lifecycle(checked_in_rules()) == []
    assert any(
        rule["ID"] == "abort-incomplete-multipart" and not offsite._rule_prefix(rule)
        for rule in checked_in_rules()
    )


def test_a_disabled_rule_is_not_a_finding():
    rules = checked_in_rules() + [
        {"ID": "someday", "Status": "Disabled", "Filter": {"Prefix": ""},
         "Expiration": {"Days": 1}}
    ]
    assert offsite.audit_lifecycle(rules) == []


@pytest.mark.parametrize("missing", ["expire-daily-dumps", "expire-weekly-dumps"])
def test_a_dump_with_no_rule_would_never_be_rotated(missing):
    """The quiet failure: the retention window silently becomes "forever", and
    the bill and the blast radius both grow without anything reporting it."""
    rules = [rule for rule in checked_in_rules() if rule["ID"] != missing]
    assert any("never be rotated" in f for f in offsite.audit_lifecycle(rules))


def test_the_probe_prefix_is_covered():
    """Bindery cannot delete what the connection test writes, so the bucket has
    to. Without this rule, one immortal object accumulates per press."""
    rules = [r for r in checked_in_rules() if r["ID"] != "expire-connection-probes"]
    assert any("connection probe" in f for f in offsite.audit_lifecycle(rules))


def test_the_sample_keys_come_from_the_real_key_builders():
    """A hard-coded `dumps/daily/...` would keep passing after the key format
    changed — which is the drift this whole guard exists to catch."""
    keys = offsite.sample_keys()
    assert keys["blob"] == offsite.object_key_for_blob("a" * 64)
    assert keys["daily dump"].startswith(offsite.DUMP_PREFIX)
    assert keys["daily manifest"].startswith(offsite.MANIFEST_PREFIX)
    assert set(keys) == set(offsite.EXPECTED_EXPIRY), (
        "a new kind of object was added without deciding what expires it"
    )


def test_moving_the_dump_prefix_without_moving_the_rule_is_caught():
    """The other direction: the rules are untouched and the code changes."""
    original = offsite.DUMP_PREFIX
    try:
        offsite.DUMP_PREFIX = "archives/"
        assert any("never be rotated" in f
                   for f in offsite.audit_lifecycle(checked_in_rules()))
    finally:
        offsite.DUMP_PREFIX = original
