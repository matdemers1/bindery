"""The worker loop's failure behaviour.

A slot that dies takes a third of the pipeline with it and leaves the container
*looking* healthy: it stays up, the health check passes, and jobs simply stop
being picked up. That is the silent failure invariant 8 exists to prevent, and
it is not hypothetical — it happened, when a schema change under a live
connection invalidated asyncpg's cached enum type OIDs and every claim began to
raise.
"""

import asyncio
import uuid

import pytest

from api.db.enums import JobStage
from api.queue import ClaimedJob
from worker import runner


def _job() -> ClaimedJob:
    return ClaimedJob(
        id=uuid.uuid4(),
        stage=JobStage.NORMALIZE,
        source_file_id=uuid.uuid4(),
        document_id=None,
        prompt_version=None,
        attempts=1,
    )


async def test_a_failing_claim_does_not_kill_the_slot(monkeypatch) -> None:
    """The loop backs off and carries on, rather than exiting silently."""
    attempts = {"count": 0}
    processed: list[uuid.UUID] = []
    stopping = asyncio.Event()

    async def flaky_claim(session, stages, worker_id):
        attempts["count"] += 1
        if attempts["count"] <= 3:
            raise RuntimeError("cached type OID no longer exists")
        stopping.set()
        return None

    monkeypatch.setattr(runner.queue, "claim", flaky_claim)
    monkeypatch.setattr(runner, "IDLE_POLL_SECONDS", 0.01)

    await asyncio.wait_for(
        runner._slot("test-worker", stopping, set()), timeout=5.0
    )

    # It kept trying rather than dying on the first failure.
    assert attempts["count"] == 4
    assert processed == []


async def test_the_slot_backs_off_after_repeated_failures(monkeypatch) -> None:
    """A database that is down must not be hammered."""
    delays: list[float] = []
    stopping = asyncio.Event()
    attempts = {"count": 0}

    async def always_fails(session, stages, worker_id):
        attempts["count"] += 1
        if attempts["count"] >= 4:
            stopping.set()
        raise RuntimeError("still down")

    real_wait_for = asyncio.wait_for

    async def recording_wait_for(awaitable, timeout):
        delays.append(timeout)
        return await real_wait_for(awaitable, 0.01)

    monkeypatch.setattr(runner.queue, "claim", always_fails)
    monkeypatch.setattr(runner, "IDLE_POLL_SECONDS", 1.0)
    monkeypatch.setattr(runner.asyncio, "wait_for", recording_wait_for)

    await real_wait_for(runner._slot("test-worker", stopping, set()), timeout=5.0)

    # Exponential, and capped so it never stops retrying altogether.
    assert delays[:3] == [2.0, 4.0, 8.0]
    assert all(delay <= 60.0 for delay in delays)


async def test_an_unknown_stage_is_dead_lettered_not_silently_succeeded(
    session, monkeypatch
) -> None:
    """A job naming a stage this build does not implement must fail loudly."""
    recorded: dict[str, object] = {}

    async def capture_fail(session_, job_id, attempts, error, *, permanent=False):
        recorded["error"] = error
        from api.db.enums import JobState

        return JobState.DEAD_LETTER

    async def capture_succeed(session_, job_id):
        recorded["succeeded"] = True

    monkeypatch.setattr(runner.queue, "fail", capture_fail)
    monkeypatch.setattr(runner.queue, "succeed", capture_succeed)
    monkeypatch.setitem(runner.STAGES, JobStage.NORMALIZE, None)
    runner.STAGES.pop(JobStage.NORMALIZE)

    try:
        await runner._run_one(_job())
    finally:
        from worker.stages.normalize import run_normalize

        runner.STAGES[JobStage.NORMALIZE] = run_normalize

    assert "succeeded" not in recorded
    assert "no implementation for stage normalize" in str(recorded["error"])


@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGINT"])
def test_shutdown_signals_are_handled(signal_name) -> None:
    """Both are registered, so a `docker stop` drains rather than truncates."""
    import inspect

    source = inspect.getsource(runner.main)
    assert signal_name in source
