"""Nobody has to remember to run the lifecycle audit any more (CR-099, R-21).

`audit_lifecycle` existed and was correct, and the only thing that ever called it
was `python -m api.cli lifecycle-check`. ADR-010 names a bucket-wide expiry rule
as able to delete the blob prefix — the archive — "with no error, no alert, and
no way for Bindery to notice, because Bindery has no delete permission and would
not be the one doing it". Guarding that with a command a human types is guarding
it with nothing: there was no schedule, no record of when it last ran, and no
alert if it had never run at all.

Every other resilience signal in this system was deliberately moved onto the
health panel and through the notifier for exactly this reason — a screen only
helps someone who opens it. This is the same move, one step later.

`tests/test_offsite_lifecycle.py` covers what the audit *finds*. This covers that
something runs it and that what it finds leaves the building.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from api import notify, offsite
from worker import runner

DEPLOYED = Path(__file__).resolve().parent.parent / "infra" / "aws" / "lifecycle.json"


def checked_in_rules() -> list[dict]:
    return json.loads(DEPLOYED.read_text())["Rules"]


def bucket_wide_expiry() -> list[dict]:
    return [
        *checked_in_rules(),
        {
            "ID": "tidy-up",
            "Status": "Enabled",
            "Filter": {},
            "Expiration": {"Days": 30},
        },
    ]


# --------------------------------------------------------------------------
# Severity: which findings are worth waking someone for
# --------------------------------------------------------------------------


def test_a_rule_that_would_delete_the_archive_is_critical():
    """`Notifier.dispatch` sends only critical alerts, so this classification is
    the difference between a phone buzzing and a line on a screen nobody has
    opened since March."""
    findings = offsite.audit_lifecycle_detailed(bucket_wide_expiry())
    deleting = [f for f in findings if f.severity == "critical"]
    assert deleting, "a prefix-less expiry rule is the one that erases the archive"
    assert any("prefix filter" in f.message for f in deleting)


def test_a_rule_that_merely_fails_to_rotate_is_a_warning():
    """Dumps accumulating forever is a bill and an untidy bucket. Nothing is
    lost, and paging for it teaches people this channel is noise."""
    rules = [rule for rule in checked_in_rules() if rule["ID"] != "expire-daily-dumps"]
    findings = offsite.audit_lifecycle_detailed(rules)
    assert findings, "removing the daily rotation rule has to be noticed"
    assert {f.severity for f in findings} == {"warning"}


def test_the_sentences_are_unchanged_for_every_existing_caller():
    """`make lifecycle-check` prints these, and `audit_lifecycle` is what it
    calls. Splitting severities out must not have moved a word."""
    rules = bucket_wide_expiry()
    assert offsite.audit_lifecycle(rules) == [
        finding.message for finding in offsite.audit_lifecycle_detailed(rules)
    ]
    assert offsite.audit_lifecycle(checked_in_rules()) == []


# --------------------------------------------------------------------------
# Reading the live bucket, including when it cannot be read
# --------------------------------------------------------------------------


class FakeClient:
    def __init__(self, rules=None, error=None):
        self._rules = rules
        self._error = error

    def get_bucket_lifecycle_configuration(self, Bucket):  # boto3 names it this way
        if self._error is not None:
            raise self._error
        return {"Rules": self._rules}


def client_error(code: str):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": code}}, "GetBucketLifecycle")


CONFIG = offsite.Config(
    access_key_id="AKIAIOSFODNN7EXAMPLE",
    secret_access_key="secret",
    bucket="bindery-offsite-d3cloud",
    region="us-east-1",
    kms_key_id="alias/bindery-offsite",
)


def test_a_healthy_bucket_produces_no_alert():
    audit = offsite.check_lifecycle(FakeClient(rules=checked_in_rules()), CONFIG)
    assert audit.findings == ()
    assert audit.unreadable is None
    assert offsite.lifecycle_alerts(audit) == []


def test_a_bucket_wide_expiry_pages():
    audit = offsite.check_lifecycle(FakeClient(rules=bucket_wide_expiry()), CONFIG)
    alerts = offsite.lifecycle_alerts(audit)
    assert [alert.severity for alert in alerts] == ["critical"]
    assert alerts[0].code == "offsite_lifecycle_deletes"
    # And it is the kind of alert that leaves the building: `dispatch` returns a
    # delivery only for the alerts it would send, and it sends only criticals.
    notifier = notify.Notifier("")
    assert [d.code for d in notifier.dispatch(alerts)] == ["offsite_lifecycle_deletes"]


def test_a_credential_that_cannot_read_the_rules_says_so_rather_than_passing():
    """The shape of every silent monitoring failure: a check that examines
    nothing and reports nothing wrong. `AccessDenied` must not look like a clean
    bucket."""
    audit = offsite.check_lifecycle(FakeClient(error=client_error("AccessDenied")), CONFIG)
    assert audit.unreadable == "AccessDenied"
    assert audit.findings == ()
    alerts = offsite.lifecycle_alerts(audit)
    assert [alert.code for alert in alerts] == ["offsite_lifecycle_unreadable"]
    assert alerts[0].severity == "warning"


def test_a_bucket_with_no_rules_at_all_is_reported_not_ignored():
    """No lifecycle configuration means dumps accumulate forever. "Nothing
    wrong" would be the wrong answer, and boto3 raises rather than returning an
    empty list for it."""
    audit = offsite.check_lifecycle(
        FakeClient(error=client_error("NoSuchLifecycleConfiguration")), CONFIG
    )
    assert audit.unreadable is None
    assert audit.findings, "no rules is a finding, not a clean bill of health"


def test_nothing_is_reported_before_the_first_look():
    assert offsite.lifecycle_alerts(None) == []


# --------------------------------------------------------------------------
# Something actually runs it
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def forget_the_cached_audit():
    runner._lifecycle_audit = None
    yield
    runner._lifecycle_audit = None


async def test_the_worker_audits_the_live_bucket_without_being_asked(monkeypatch):
    """The finding, in one test: `audit_lifecycle` had exactly one caller and it
    was the CLI."""
    monkeypatch.setattr(offsite, "config_from_settings", _config_always())
    monkeypatch.setattr(offsite, "make_client", lambda config: FakeClient(bucket_wide_expiry()))

    await runner._refresh_lifecycle_audit()

    assert runner._lifecycle_audit is not None
    assert [alert.code for alert in offsite.lifecycle_alerts(runner._lifecycle_audit)] == [
        "offsite_lifecycle_deletes"
    ]


async def test_an_unconfigured_bucket_is_not_audited_and_not_alerted(monkeypatch):
    """`_offsite_alerts` already says "no offsite copy is configured". A second
    sentence about the lifecycle rules of a bucket that does not exist is the
    noise that gets a channel muted."""
    empty = offsite.Config(
        access_key_id=None, secret_access_key=None, bucket=None, region=None, kms_key_id=None
    )

    async def config(session):
        return empty

    def refuse(config):  # pragma: no cover — the point is that it is not reached
        raise AssertionError("an unconfigured bucket must not be contacted")

    monkeypatch.setattr(offsite, "config_from_settings", config)
    monkeypatch.setattr(offsite, "make_client", refuse)

    await runner._refresh_lifecycle_audit()
    assert runner._lifecycle_audit is None
    assert offsite.lifecycle_alerts(runner._lifecycle_audit) == []


async def test_it_does_not_ask_the_bucket_again_every_ten_minutes(monkeypatch):
    """The replication loop wakes every ten minutes; the rules change when a
    human edits a console. Hourly, so a rule added in the morning is alarming by
    lunchtime without signing 144 requests a day."""
    calls = []

    def count(config):
        calls.append(config)
        return FakeClient(checked_in_rules())

    monkeypatch.setattr(offsite, "config_from_settings", _config_always())
    monkeypatch.setattr(offsite, "make_client", count)

    await runner._refresh_lifecycle_audit()
    await runner._refresh_lifecycle_audit()
    assert len(calls) == 1, "the second pass re-read the bucket inside the interval"

    runner._lifecycle_audit = offsite.LifecycleAudit(
        checked_at=datetime.now(UTC) - offsite.LIFECYCLE_AUDIT_INTERVAL - timedelta(minutes=1)
    )
    await runner._refresh_lifecycle_audit()
    assert len(calls) == 2, "an audit older than the interval has to be refreshed"


async def test_a_bucket_that_cannot_be_reached_never_stops_a_copy_leaving(monkeypatch):
    """Notification failure is never pipeline failure, and this is the same rule
    one layer out: a lifecycle rule Bindery cannot read must not be able to stop
    replication."""

    def explode(config):
        raise OSError("no route to host")

    monkeypatch.setattr(offsite, "config_from_settings", _config_always())
    monkeypatch.setattr(offsite, "make_client", explode)

    with pytest.raises(OSError):
        await runner._refresh_lifecycle_audit()
    # …which is why the loop wraps it. The call site's own try is asserted
    # structurally below, because driving the whole loop needs a live bucket.
    source = Path(runner.__file__).read_text()
    guarded = source.split("await _refresh_lifecycle_audit()")[0]
    assert guarded.rstrip().endswith("try:"), (
        "the lifecycle audit is called outside a try in the replication loop — a "
        "bucket that cannot be read would stop the copy leaving the building"
    )


def test_the_audit_reports_and_never_acts():
    """Invariant 3, and ADR-010's version of it: expiry is performed by S3
    lifecycle rules and never by Bindery, which holds no delete permission. The
    audit gained a scheduler, not a remedy."""
    source = Path(offsite.__file__).read_text()
    watch = source.split("# The lifecycle audit")[1]
    for forbidden in ("put_bucket_lifecycle_configuration", "delete_bucket_lifecycle"):
        assert forbidden not in watch, (
            f"the lifecycle audit calls {forbidden} — it reports on the rules and "
            "must never write them"
        )


def _config_always():
    async def config(session):
        return CONFIG

    return config
