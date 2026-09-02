"""What the Trust screen says about the offsite copy (T-13.7, REQ-164).

The screen's job is to be believed, so the interesting assertions here are all
about refusing to claim more than is known:

- an **age**, not a tick, because a tick is a claim that stops being checked;
- **stale when nothing has ever succeeded**, because an empty history is the
  most alarming state this can be in, not the most neutral;
- **a failure is a row with a reason**, not a gap where a success would be.
"""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from api import offsite_runs, settings_store
from api.db.models import OffsiteRun, Setting
from api.offsite import Kind


@pytest.fixture(autouse=True)
async def clean(session):
    await session.execute(sa.delete(OffsiteRun))
    await session.execute(sa.delete(Setting))
    await session.commit()


async def as_administrator(session, signed_in):
    """Asking for a run is the administrator's (CR-006, ADR-009).

    A run pg_dumps every library and ships every blob and every vault object,
    so it is a host operation rather than a library one. Reading the *status*
    stays open to every member, and the tests above still sign in as one.
    """
    user, library = await signed_in()
    user.is_admin = True
    await session.commit()
    return user, library


async def configure(session, user_id=None):
    for key, value in (
        (settings_store.AWS_ACCESS_KEY_ID, "AKIAIOSFODNN7EXAMPLE"),
        (settings_store.AWS_SECRET_ACCESS_KEY, "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
        (settings_store.OFFSITE_BUCKET, "bindery-offsite-d3cloud"),
        (settings_store.OFFSITE_REGION, "us-east-1"),
        (settings_store.OFFSITE_KMS_KEY_ID, "alias/bindery-offsite"),
    ):
        await settings_store.set_(session, key, value, actor_id=user_id)
    await session.commit()


async def add_run(session, kind, *, state, at, detail=None, failures=None):
    run = OffsiteRun(
        kind=kind.value, state=state, trigger="schedule", started_at=at,
        finished_at=at + timedelta(minutes=2), detail=detail, failures=failures,
    )
    session.add(run)
    await session.commit()
    return run


async def test_an_unconfigured_archive_reports_stale_not_green(client, signed_in):
    """Nothing has ever left the building. That is the alarming state."""
    await signed_in()
    body = (await client.get("/api/offsite")).json()
    assert body["configured"] is False
    assert body["last_success_at"] is None
    assert body["stale"] is True


async def test_the_screen_reports_an_age_rather_than_a_tick(client, session, signed_in):
    await signed_in()
    await configure(session)
    await add_run(session, Kind.DAILY, state=offsite_runs.SUCCEEDED,
                  at=datetime.now(UTC) - timedelta(hours=5))

    body = (await client.get("/api/offsite")).json()
    assert body["stale"] is False
    # Roughly five hours, not a boolean.
    assert 17_000 < body["last_success_age_seconds"] < 19_000


async def test_a_failed_run_appears_with_its_reason(client, session, signed_in):
    """A failure is a row with a reason, not a gap where a success would be."""
    await signed_in()
    await configure(session)
    await add_run(
        session, Kind.DAILY, state=offsite_runs.FAILED, at=datetime.now(UTC),
        detail="3 blob(s) failed to upload — the dump was not sent",
        failures=["abc123: Denied.", "def456: Denied."],
    )

    body = (await client.get("/api/offsite")).json()
    assert body["stale"] is True, "a failed run must not read as a recent success"
    run = body["runs"][0]
    assert run["state"] == "failed"
    assert "the dump was not sent" in run["detail"]
    assert len(run["failures"]) == 2


async def test_the_history_is_newest_first(client, session, signed_in):
    await signed_in()
    await configure(session)
    now = datetime.now(UTC)
    await add_run(session, Kind.WEEKLY, state=offsite_runs.SUCCEEDED,
                  at=now - timedelta(days=3), detail="older")
    await add_run(session, Kind.DAILY, state=offsite_runs.SUCCEEDED,
                  at=now - timedelta(hours=1), detail="newer")

    runs = (await client.get("/api/offsite")).json()["runs"]
    assert [run["detail"] for run in runs] == ["newer", "older"]


async def test_replicate_now_queues_a_run_rather_than_uploading(client, session, signed_in):
    """The api never uploads. A few hundred megabytes inside a request handler
    holds a connection open for minutes and dies with the request."""
    await as_administrator(session, signed_in)
    await configure(session)

    response = await client.post("/api/offsite/replicate")
    assert response.status_code == 200
    assert response.json()["in_flight"] == "requested"

    run = (await session.execute(sa.select(OffsiteRun))).scalars().one()
    assert run.state == offsite_runs.REQUESTED
    assert run.trigger == "manual"
    assert run.requested_by is not None


async def test_two_presses_do_not_become_two_uploads(client, session, signed_in):
    """With versioning on and no delete permission, a duplicate object version
    is permanent."""
    await as_administrator(session, signed_in)
    await configure(session)

    assert (await client.post("/api/offsite/replicate")).status_code == 200
    second = await client.post("/api/offsite/replicate")
    assert second.status_code == 409
    assert "already queued or in progress" in second.text

    count = (await session.execute(sa.select(sa.func.count(OffsiteRun.id)))).scalar_one()
    assert count == 1


async def test_replicate_now_is_refused_when_nothing_is_configured(
    client, session, signed_in
):
    """And names what is missing, rather than queueing a run that would fail."""
    await as_administrator(session, signed_in)
    response = await client.post("/api/offsite/replicate")
    assert response.status_code == 409
    assert "not configured" in response.text
    assert "bucket" in response.text


async def test_the_request_is_audited(client, session, signed_in):
    from api.db.models import AuditEvent

    user, _ = await as_administrator(session, signed_in)
    await configure(session)
    await client.post("/api/offsite/replicate")

    event = (
        await session.execute(
            sa.select(AuditEvent)
            .where(AuditEvent.action == "offsite_replicate_requested",
                   AuditEvent.actor_id == user.id)
            .order_by(AuditEvent.sequence.desc())
        )
    ).scalars().first()
    assert event is not None
    assert event.after == {"kind": "daily"}


async def test_a_weekly_run_can_be_asked_for_explicitly(client, session, signed_in):
    await as_administrator(session, signed_in)
    await configure(session)
    assert (await client.post("/api/offsite/replicate?kind=weekly")).status_code == 200
    run = (await session.execute(sa.select(OffsiteRun))).scalars().one()
    assert run.kind == "weekly"


async def test_an_unknown_kind_is_refused(client, session, signed_in):
    await as_administrator(session, signed_in)
    await configure(session)
    assert (await client.post("/api/offsite/replicate?kind=hourly")).status_code == 422


async def test_the_status_needs_a_signed_in_user(client):
    assert (await client.get("/api/offsite")).status_code == 401
