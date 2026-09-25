"""Phase 8 — Q&A, health and notifications (T-8.1, T-8.2, T-8.3).

The two failures this phase defends against are opposites, and both are quiet:

- **An uncited answer.** A confident summary of someone's VA records with no
  source is worse than no feature, because a plausible wrong answer gets acted
  on. So an answer without citations is discarded, not shown with a caveat.
- **A stopped pipeline.** Nothing fails, nothing complains, and a document you
  scanned never made it in — discovered years later, when you go looking.
"""

import typing
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from api import ask, health_panel, notify
from api.ai_ask import AskRequest, AskResponse, AskSource, Citation, build_blocks
from api.db.enums import IngestSource, JobStage, JobState, SourceFileState
from api.db.models import Classification, Document, Job, Page, SourceFile
from api.health_panel import Alert


@pytest.fixture
async def brake_receipt(session, signed_in):
    """The exit demo's question needs a document that can answer it."""
    _, library = await signed_in()
    source_file = SourceFile(
        library_id=library.id, sha256=uuid.uuid4().hex * 2, byte_size=100,
        original_filename="valvoline.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        page_count=2, state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    await session.flush()
    session.add_all([
        Page(source_file_id=source_file.id, page_number=1,
             text="Valvoline Instant Oil Change. Invoice 88214."),
        Page(source_file_id=source_file.id, page_number=2,
             text="Front brake pads replaced 2025-11-03. Parts and labour $412.60."),
    ])
    document = Document(
        library_id=library.id, source_file_id=source_file.id,
        page_start=1, page_end=2, title="Valvoline service invoice",
    )
    session.add(document)
    await session.commit()
    return library, document


class FakeAnswerer:
    """A provider stub whose citations are the thing under test."""

    def __init__(self, answer: str, cite_indexes: list[int] | None = None) -> None:
        self._answer = answer
        self._cite = cite_indexes if cite_indexes is not None else [0]
        self.seen: AskRequest | None = None

    def available(self) -> bool:
        return True

    async def answer(self, request: AskRequest) -> AskResponse:
        self.seen = request
        citations = [
            Citation(
                document_id=request.sources[i].document_id,
                source_file_id=request.sources[i].source_file_id,
                title=request.sources[i].title,
                page_number=request.sources[i].page_number,
                quote=request.sources[i].text[:40],
            )
            for i in self._cite
            if i < len(request.sources)
        ]
        return AskResponse(self._answer, citations, "claude-opus-5")


# --------------------------------------------------------------------------
# T-8.1 — Q&A with citations (REQ-116)
# --------------------------------------------------------------------------


async def test_an_answer_carries_the_page_it_came_from(session, brake_receipt) -> None:
    """The exit demo, exactly: ask about the brakes, get the receipt and page."""
    library, document = brake_receipt
    provider = FakeAnswerer("Front brake pads were replaced on 3 November 2025.", [1])

    result = await ask.ask(session, "when did I last get the brakes done?", [library.id], provider)

    assert result.answer
    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation.document_id == str(document.id)
    assert citation.page_number == 2, "the answer is on page 2, not page 1"


async def test_an_uncited_answer_is_discarded_not_shown(session, brake_receipt) -> None:
    """The rule of this phase. A caveat is read once; an answer is believed."""
    library, _ = brake_receipt
    provider = FakeAnswerer("Your brakes are fine.", cite_indexes=[])

    result = await ask.ask(session, "when did I last get the brakes done?", [library.id], provider)

    assert result.answer is None, "an uncited answer must not reach the caller"
    assert "not going to give you one" in (result.unavailable_reason or "")
    # But the retrieval is still handed back — the pages are real even when the
    # answer was not.
    assert result.consulted


async def test_a_citation_outside_the_supplied_pages_is_dropped(session, brake_receipt) -> None:
    """A citation index we never sent is not evidence of anything."""
    library, _ = brake_receipt
    provider = FakeAnswerer("Answer.", cite_indexes=[99])

    result = await ask.ask(session, "brakes", [library.id], provider)
    assert result.answer is None


async def test_with_no_api_key_ask_still_returns_the_pages(session, brake_receipt) -> None:
    """Invariant 7 as a product behaviour: retrieval never needs the API."""
    library, document = brake_receipt

    result = await ask.ask(session, "brake pads", [library.id], None)

    assert result.answer is None
    assert "No API key" in (result.unavailable_reason or "")
    assert [entry["document_id"] for entry in result.consulted] == [str(document.id)] * len(
        result.consulted
    )
    assert result.consulted, "the search results are still useful on their own"


async def test_a_provider_that_raises_degrades_to_retrieval(session, brake_receipt) -> None:
    library, _ = brake_receipt

    class Broken:
        def available(self) -> bool:
            return True

        async def answer(self, request):
            raise RuntimeError("upstream is down")

    result = await ask.ask(session, "brake pads", [library.id], Broken())
    assert result.answer is None
    assert "upstream is down" in (result.unavailable_reason or "")
    assert result.consulted


async def test_nothing_matching_says_so_without_calling_the_model(
    session, brake_receipt
) -> None:
    library, _ = brake_receipt
    provider = FakeAnswerer("I should not have been called.")

    result = await ask.ask(session, "zzzznonexistentzzzz", [library.id], provider)
    assert result.answer is None
    assert provider.seen is None, "no sources means no reason to call the API"


async def test_the_model_only_sees_pages_from_the_askers_libraries(
    session, brake_receipt, signed_in
) -> None:
    library, _ = brake_receipt
    _, elsewhere = await signed_in()
    other_file = SourceFile(
        library_id=elsewhere.id, sha256=uuid.uuid4().hex * 2, byte_size=10,
        original_filename="other.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1, state=SourceFileState.PROCESSED,
    )
    session.add(other_file)
    await session.flush()
    session.add(Page(source_file_id=other_file.id, page_number=1,
                     text="brake pads replaced on someone else's car"))
    session.add(Document(library_id=elsewhere.id, source_file_id=other_file.id,
                         page_start=1, page_end=1, title="Not yours"))
    await session.commit()

    provider = FakeAnswerer("Answer.")
    await ask.ask(session, "brake pads", [library.id], provider)

    assert provider.seen is not None
    titles = {source.title for source in provider.seen.sources}
    assert "Not yours" not in titles


def test_each_page_is_its_own_citable_block() -> None:
    """One block per page is what makes the cited page number exact."""
    request = AskRequest(
        question="q",
        sources=[
            AskSource("d1", "f1", "Invoice", 1, "page one"),
            AskSource("d1", "f1", "Invoice", 2, "page two"),
        ],
    )
    blocks = build_blocks(request)
    assert len(blocks) == 2
    assert all(block["citations"] == {"enabled": True} for block in blocks)
    assert "page 2" in blocks[1]["title"]


# --------------------------------------------------------------------------
# T-8.2 — the health panel (REQ-109)
# --------------------------------------------------------------------------


@pytest.fixture
async def empty_queue(session):
    """The health panel is instance-wide by design, so a test of it must own
    the instance. Tests are exempt from the never-delete rule that governs
    `api/` and `worker/` — this is the queue table, not anybody's documents."""
    await session.execute(sa.delete(Job))
    await session.commit()
    yield




async def test_the_panel_reports_a_seeded_failure(session, empty_queue, brake_receipt) -> None:
    _, document = brake_receipt
    session.add(
        Job(source_file_id=document.source_file_id, stage=JobStage.CLASSIFY,
            state=JobState.DEAD_LETTER, attempts=5, last_error="boom")
    )
    await session.commit()

    panel = await health_panel.collect(session)
    assert panel.dead_letter >= 1
    assert not panel.healthy
    assert any(alert.code == "dead_letter" for alert in panel.alerts)


async def test_a_stuck_job_is_reported_as_a_dead_worker(
    session, empty_queue, brake_receipt
) -> None:
    """A held lock is the dangerous state: nothing failed, nothing finished."""
    _, document = brake_receipt
    session.add(
        Job(
            source_file_id=document.source_file_id, stage=JobStage.OCR
            if hasattr(JobStage, "OCR") else JobStage.PAGE,
            state=JobState.RUNNING, locked_by="worker-1",
            locked_at=datetime.now(UTC) - timedelta(hours=2),
        )
    )
    await session.commit()

    panel = await health_panel.collect(session)
    assert len(panel.stuck_jobs) == 1
    assert panel.stuck_jobs[0]["locked_by"] == "worker-1"
    assert any(alert.code == "stuck" for alert in panel.alerts)
    assert not panel.healthy


async def test_a_stall_is_queued_work_with_nothing_running(
    session, empty_queue, brake_receipt
) -> None:
    """The failure that never announces itself."""
    _, document = brake_receipt
    session.add(
        Job(
            source_file_id=document.source_file_id, stage=JobStage.CLASSIFY,
            state=JobState.QUEUED,
            scheduled_for=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    await session.commit()

    panel = await health_panel.collect(session)
    assert panel.stalled is True
    assert any(alert.code == "stalled" for alert in panel.alerts)


async def test_a_healthy_queue_is_not_a_stall(session, empty_queue, brake_receipt) -> None:
    """Work queued a moment ago is a working pipeline, not a stopped one."""
    _, document = brake_receipt
    session.add(
        Job(source_file_id=document.source_file_id, stage=JobStage.CLASSIFY,
            state=JobState.QUEUED)
    )
    await session.commit()

    panel = await health_panel.collect(session)
    assert panel.stalled is False
    assert panel.queue_depth.get("classify", 0) >= 1


async def test_spend_counts_cache_reads_at_the_cheap_rate(session, brake_receipt) -> None:
    """Otherwise a healthy cache would look like a spending problem."""
    fresh = health_panel.estimate_cost({"input_tokens": 1_000_000})
    cached = health_panel.estimate_cost({"cache_read_input_tokens": 1_000_000})
    assert fresh == pytest.approx(5.0)
    assert cached == pytest.approx(0.5)
    assert health_panel.estimate_cost({}) == 0.0


async def test_spend_is_totalled_from_recorded_usage(session, brake_receipt) -> None:
    _, document = brake_receipt
    session.add(
        Classification(
            document_id=document.id, model="claude-opus-5", prompt_version="v1",
            usage={"input_tokens": 200_000, "output_tokens": 40_000},
        )
    )
    await session.commit()

    panel = await health_panel.collect(session)
    assert panel.spend_30d_usd >= 1.0
    assert panel.spend_by_day


async def test_the_panel_endpoint_needs_authentication(client) -> None:
    """Queue contents and API spend are not facts to hand an anonymous caller."""
    assert (await client.get("/api/health/panel")).status_code == 401
    # The liveness probe stays open, because the container healthcheck calls it.
    assert (await client.get("/api/health")).status_code == 200


async def test_liveness_reports_the_database_schema_revision(client, session) -> None:
    """BND-T-001 — Shipyard's deploy contract.

    Shipyard runs `alembic upgrade head` as a one-shot before swapping to a new
    image, then checks `/api/health` for the schema it landed on. `schema` must
    be the database's actual `alembic_version.version_num`, not a value derived
    from the running process's own code — the two can legitimately differ for
    the seconds between "the one-shot committed" and "the swap completed", and
    that gap is exactly what this field exists to make visible.
    """
    applied = (
        await session.execute(sa.text("select version_num from alembic_version"))
    ).scalar_one()

    body = (await client.get("/api/health")).json()
    assert body["status"] == "ok"
    assert body["schema"] == applied


# --------------------------------------------------------------------------
# T-8.3 — notifications (REQ-110)
# --------------------------------------------------------------------------


class RecordingNotifier(notify.Notifier):
    def __init__(self, **kwargs) -> None:
        super().__init__("https://push.example.test/bindery", **kwargs)
        self.sent: list[str] = []

    def send(self, alert, *, now=None):
        now = now or datetime.now(UTC)
        if not self.should_send(alert.code, now=now):
            return notify.Delivery(alert.code, False, "within cooldown")
        self._last_sent[alert.code] = now
        self.sent.append(alert.code)
        return notify.Delivery(alert.code, True)


def test_a_stall_produces_a_notification() -> None:
    notifier = RecordingNotifier()
    notifier.dispatch([Alert("critical", "stalled", "The pipeline has stopped.")])
    assert notifier.sent == ["stalled"]


def test_a_continuing_stall_does_not_notify_every_pass() -> None:
    """A notification you learn to ignore is worse than none."""
    notifier = RecordingNotifier()
    alerts = [Alert("critical", "stalled", "stopped")]
    for _ in range(5):
        notifier.dispatch(alerts)
    assert notifier.sent == ["stalled"]


def test_a_recurrence_after_it_clears_notifies_again() -> None:
    notifier = RecordingNotifier()
    alerts = [Alert("critical", "stalled", "stopped")]
    notifier.dispatch(alerts)
    notifier.dispatch([])          # resolved
    notifier.dispatch(alerts)      # and back again
    assert notifier.sent == ["stalled", "stalled"]


def test_warnings_stay_on_the_panel_and_do_not_push() -> None:
    """A few failed jobs is something to see next time you look."""
    notifier = RecordingNotifier()
    notifier.dispatch([Alert("warning", "recent_failures", "3 jobs failed")])
    assert notifier.sent == []


def test_an_unreachable_webhook_never_breaks_the_pipeline() -> None:
    """A notifier that can take the archive down with it is a worse problem."""
    notifier = notify.Notifier("http://127.0.0.1:9/definitely-not-listening")
    delivery = notifier.send(Alert("critical", "stalled", "stopped"))
    assert delivery.sent is False
    assert delivery.reason


def test_with_no_webhook_configured_nothing_is_attempted() -> None:
    notifier = notify.Notifier("")
    delivery = notifier.send(Alert("critical", "stalled", "stopped"))
    assert delivery.sent is False
    assert delivery.reason == "no webhook configured"


# --------------------------------------------------------------------------
# Thinking has to be configured, not left to the default (T-9.3)
# --------------------------------------------------------------------------


class _Recorder:
    """Captures the kwargs a request was actually built with."""

    def __init__(self) -> None:
        self.kwargs: dict = {}

    class _Messages:
        def __init__(self, outer) -> None:
            self.outer = outer

        async def create(self, **kwargs):
            self.outer.kwargs = kwargs

            class Response:
                content: typing.ClassVar[list] = []
                stop_reason = "end_turn"

                class usage:
                    @staticmethod
                    def model_dump():
                        return {}

            return Response()

    @property
    def messages(self):
        return self._Messages(self)


async def test_a_plain_completion_turns_thinking_off() -> None:
    """Omitting `thinking` runs adaptive, and adaptive expands to fill
    `max_tokens` — the first unify run spent all 16,000 tokens reasoning and
    returned an empty string.

    Measured on the deployed archive, thinking also made the answer *worse*:
    11 groups found with it off, 1 with it on at low effort.
    """
    from api.ai_ask import NO_THINKING, ClaudeAnswerer

    recorder = _Recorder()
    answerer = ClaudeAnswerer("", model="claude-sonnet-5", client=recorder)
    await answerer.complete("group these", max_tokens=16000)

    assert recorder.kwargs["thinking"] == NO_THINKING
    assert recorder.kwargs["max_tokens"] == 16000


async def test_asking_bounds_thinking_rather_than_leaving_it_to_the_default() -> None:
    """At the old 2,000-token ceiling, adaptive reasoning could eat the whole
    budget and return nothing — which `/api/ask` discards as uncited, so the
    reader is told "I could not find that" about an answerable question."""
    from api.ai_ask import ASK_MAX_TOKENS, AskRequest, AskSource, ClaudeAnswerer

    recorder = _Recorder()
    answerer = ClaudeAnswerer("", model="claude-sonnet-5", client=recorder)
    await answerer.answer(
        AskRequest(
            question="when were the brakes done?",
            sources=[
                AskSource(
                    document_id="d", source_file_id="s", title="Receipt",
                    page_number=1, text="Brake pads replaced 4 March 2024.",
                )
            ],
        )
    )

    assert recorder.kwargs["thinking"]["type"] == "adaptive"
    assert recorder.kwargs["output_config"] == {"effort": "low"}
    assert ASK_MAX_TOKENS >= 8000, "room for the answer after the reasoning"
