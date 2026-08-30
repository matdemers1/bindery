"""Replication staleness on the health panel (T-13.8, REQ-165, REQ-110).

The Trust screen already shows all of this, and a screen only helps someone who
opens it. The failure this exists to catch is replication that quietly stopped
in March and is noticed in November — so it raises the same alert a stalled
pipeline does, on the same panel, through the same notifier.

Severity is doing real work, because `Notifier.dispatch` sends **only critical
alerts**. That makes the difference between "visible to anyone looking" and
"pages you at 3am" a deliberate choice per state rather than an accident.
"""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from api import health_panel, notify, offsite_runs, settings_store
from api.db.models import OffsiteRun, Setting

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def clean(session):
    await session.execute(sa.delete(OffsiteRun))
    await session.execute(sa.delete(Setting))
    await session.commit()


async def configure(session):
    for key, value in (
        (settings_store.AWS_ACCESS_KEY_ID, "AKIAIOSFODNN7EXAMPLE"),
        (settings_store.AWS_SECRET_ACCESS_KEY, "secret"),
        (settings_store.OFFSITE_BUCKET, "bindery-offsite-d3cloud"),
        (settings_store.OFFSITE_REGION, "us-east-1"),
        (settings_store.OFFSITE_KMS_KEY_ID, "alias/bindery-offsite"),
    ):
        await settings_store.set_(session, key, value, actor_id=None)
    await session.commit()


async def add_run(session, *, state, at, kind="daily"):
    session.add(OffsiteRun(kind=kind, state=state, trigger="schedule", started_at=at))
    await session.commit()


async def codes(session) -> dict[str, str]:
    alerts = await health_panel._offsite_alerts(session, datetime.now(UTC))
    return {alert.code: alert.severity for alert in alerts}


async def test_unconfigured_is_visible_but_does_not_page(session):
    """Every fresh install and every dev stack is in this state. A critical
    alert here would fire on first boot and teach people the channel is noise —
    but staying silent is how R-09 went two days looking closed."""
    assert await codes(session) == {"offsite_unconfigured": "warning"}


async def test_configured_and_not_yet_run_is_a_warning_not_an_alarm(session):
    """Ten-minute lifespan: the worker checks every ten minutes and a weekly run
    is due immediately on a fresh archive. Critical would be a false alarm."""
    await configure(session)
    assert await codes(session) == {"offsite_pending": "warning"}


async def test_configured_and_every_attempt_failed_is_critical(session):
    await configure(session)
    for hours in (1, 5, 9):
        await add_run(session, state=offsite_runs.FAILED,
                      at=datetime.now(UTC) - timedelta(hours=hours))

    alerts = await health_panel._offsite_alerts(session, datetime.now(UTC))
    assert [a.code for a in alerts] == ["offsite_never"]
    assert alerts[0].severity == "critical"
    assert "3 attempt(s)" in alerts[0].message


async def test_a_copy_older_than_two_days_is_critical(session):
    """REQ-165: the same alert a stalled pipeline raises."""
    await configure(session)
    await add_run(session, state=offsite_runs.SUCCEEDED,
                  at=datetime.now(UTC) - timedelta(hours=49))

    alerts = await health_panel._offsite_alerts(session, datetime.now(UTC))
    assert alerts[0].code == "offsite_stale"
    assert alerts[0].severity == "critical"
    assert "49 hours ago" in alerts[0].message


async def test_a_recent_copy_raises_nothing(session):
    await configure(session)
    await add_run(session, state=offsite_runs.SUCCEEDED,
                  at=datetime.now(UTC) - timedelta(hours=4))
    assert await codes(session) == {}


async def test_failing_now_but_not_yet_stale_warns_early(session):
    """The early warning, and the direction matters.

    A success 10 hours ago followed by two failures is not stale yet — the
    critical threshold is 48 hours — but it is on its way there. Saying so now
    buys a day and a half.
    """
    await configure(session)
    await add_run(session, state=offsite_runs.SUCCEEDED,
                  at=datetime.now(UTC) - timedelta(hours=10))
    for hours in (2, 6):
        await add_run(session, state=offsite_runs.FAILED,
                      at=datetime.now(UTC) - timedelta(hours=hours))

    alerts = await health_panel._offsite_alerts(session, datetime.now(UTC))
    assert alerts[0].code == "offsite_failing"
    assert alerts[0].severity == "warning"
    assert "2 replication run(s)" in alerts[0].message


async def test_a_run_that_failed_and_then_recovered_says_nothing(session):
    """The other direction, and the reason this pair exists.

    Written as one test that contradicted its own sibling: both had a failure
    and a later success, and both expected different answers. Splitting them
    showed the alert's wording described "recovered" while its query computed
    "still failing" — the query was the useful one, the sentence was wrong.
    """
    await configure(session)
    await add_run(session, state=offsite_runs.FAILED,
                  at=datetime.now(UTC) - timedelta(hours=6))
    await add_run(session, state=offsite_runs.SUCCEEDED,
                  at=datetime.now(UTC) - timedelta(hours=2))
    assert await codes(session) == {}, "a bad night that recovered is not an alert"


async def test_a_stale_copy_makes_the_whole_panel_unhealthy(session):
    """`HealthPanel.healthy` is "no critical alerts". An archive with no offsite
    copy for two days is not a healthy archive."""
    await configure(session)
    await add_run(session, state=offsite_runs.SUCCEEDED,
                  at=datetime.now(UTC) - timedelta(hours=60))

    panel = await health_panel.collect(session)
    stale = next(a for a in panel.alerts if a.code == "offsite_stale")
    assert stale.severity == "critical"
    # A critical alert is what makes the panel unhealthy, and an archive with no
    # offsite copy for two days is not a healthy archive.
    assert panel.healthy is False


async def test_unconfigured_contributes_no_critical_alert(session):
    """Asserted on the offsite alerts specifically, not on `panel.healthy`.

    `collect` reads the whole database, and other test files leave dead-lettered
    jobs behind that make the panel critical for reasons of their own. The first
    version of this asserted `panel.healthy is True` — which passed alone and
    failed in the suite, and would have been "fixed" by whichever test ran last.
    """
    panel = await health_panel.collect(session)
    offsite_alerts = [a for a in panel.alerts if a.code.startswith("offsite_")]
    assert [a.code for a in offsite_alerts] == ["offsite_unconfigured"]
    assert all(a.severity == "warning" for a in offsite_alerts)


def test_only_the_critical_offsite_alerts_leave_the_building():
    """The whole point of choosing severities per state.

    `dispatch` sends criticals only, so a fresh install pages nobody while a
    replication that has actually stopped does.
    """
    notifier = notify.Notifier("https://example.invalid/hook")
    quiet = [
        health_panel.Alert("warning", "offsite_unconfigured", "no offsite copy"),
        health_panel.Alert("warning", "offsite_pending", "not run yet"),
        health_panel.Alert("warning", "offsite_failing", "failing"),
    ]
    assert notifier.dispatch(quiet) == []

    loud = [health_panel.Alert("critical", "offsite_stale", "stopped")]
    assert [d.code for d in notifier.dispatch(loud)] == ["offsite_stale"]
