"""EventRecorder: redaction, correlation, batching, buffering, and publishing."""

import asyncio
import contextlib
import json
import os
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from mooo_core.bus import EVENTS_CHANNEL
from mooo_core.events import DatabaseUnavailableError, EventRecorder, PostgresEventSink
from mooo_core.log import correlation_scope
from mooo_core.models import Environment, EventCategory, EventRecord
from mooo_core.redaction import REDACTED, SecretRedactor
from mooo_core.stream import EventMessage

API_KEY = "AbCdEfGh12345678"
API_SECRET = "s3cr3tValueXYZ987654"
SESSION_TOKEN = "sess-9f8e7d6c5b4a"
SIGNATURE = "0123456789abcdef"


class FakeSink:
    def __init__(self) -> None:
        self.batches: list[list[EventMessage]] = []
        self.available = True
        self.reject_messages: set[str] = set()

    async def write(self, events: Sequence[EventMessage]) -> None:
        if not self.available:
            raise DatabaseUnavailableError("OperationalError")
        if any(event.message in self.reject_messages for event in events):
            raise ValueError("rejected")
        self.batches.append(list(events))

    @property
    def messages(self) -> list[str]:
        return [event.message for batch in self.batches for event in batch]


class FakePublisher:
    def __init__(self) -> None:
        self.frames: list[tuple[str, str]] = []
        self.fail = False

    async def publish(self, channel: str, message: str) -> int:
        if self.fail:
            raise ConnectionError("redis down")
        self.frames.append((channel, message))
        return 1


def test_defaults_match_blueprint() -> None:
    recorder = EventRecorder(Environment.DEMO)

    assert recorder.max_buffer == 10_000
    assert recorder.flush_interval_s == 0.25


async def test_events_are_redacted_before_storage_and_publish() -> None:
    sink, publisher = FakeSink(), FakePublisher()
    recorder = EventRecorder(
        Environment.DEMO,
        sink=sink,
        publisher=publisher,
        redactor=SecretRedactor([API_SECRET]),
    )

    recorder.record(
        EventCategory.SECURITY,
        f"credential saved; secret {API_SECRET} X-BAPI-API-KEY: {API_KEY}",
        refs={
            "api_key": API_KEY,
            "masked_key": "****5678",
            "headers": {"X-BAPI-SIGN": SIGNATURE},
            "cookie": f"mooo_session={SESSION_TOKEN}",
            "session_token": SESSION_TOKEN,
        },
    )
    await recorder.flush()

    [[event]] = sink.batches
    assert event.refs == {
        "api_key": REDACTED,
        "masked_key": "****5678",
        "headers": {"X-BAPI-SIGN": REDACTED},
        "cookie": REDACTED,
        "session_token": REDACTED,
    }
    [(channel, frame)] = publisher.frames
    assert channel == EVENTS_CHANNEL
    stored = event.model_dump_json()
    for secret in (API_KEY, API_SECRET, SESSION_TOKEN, SIGNATURE):
        assert secret not in stored
        assert secret not in frame
    decoded = json.loads(frame)
    assert decoded["type"] == "event"
    assert decoded["environment"] == "demo"
    assert decoded["payload"]["category"] == "SECURITY"
    assert decoded["payload"]["message"] == event.message


async def test_events_in_one_scope_share_a_correlation_id() -> None:
    recorder = EventRecorder(Environment.DEMO, sink=FakeSink())

    with correlation_scope() as correlation_id:
        proposal = recorder.record(EventCategory.STRATEGY, "proposal created")
        verdict = recorder.record(EventCategory.RISK, "risk approved")
    unrelated = recorder.record(EventCategory.INFO, "unrelated")
    explicit = uuid.uuid4()
    order = recorder.record("EXECUTION", "order placed", correlation_id=str(explicit))

    assert proposal.correlation_id == verdict.correlation_id == correlation_id
    assert unrelated.correlation_id is None
    assert order.correlation_id == explicit
    assert order.category is EventCategory.EXECUTION


async def test_writes_in_batches() -> None:
    sink = FakeSink()
    recorder = EventRecorder(Environment.DEMO, sink=sink, max_batch=2)
    for index in range(5):
        recorder.record(EventCategory.INFO, f"e{index}")

    await recorder.flush()

    assert [len(batch) for batch in sink.batches] == [2, 2, 1]
    assert sink.messages == ["e0", "e1", "e2", "e3", "e4"]
    assert recorder.pending_count == 0


async def test_buffers_while_database_is_down_and_reports_once() -> None:
    calls: list[str] = []

    async def unavailable() -> None:
        calls.append("down")

    def recovered() -> None:
        calls.append("up")

    sink = FakeSink()
    sink.available = False
    recorder = EventRecorder(
        Environment.LIVE,
        sink=sink,
        on_db_unavailable=unavailable,
        on_db_recovered=recovered,
    )
    for index in range(3):
        recorder.record(EventCategory.INFO, f"e{index}")

    await recorder.flush()
    await recorder.flush()
    assert calls == ["down"]
    assert not recorder.db_available
    assert recorder.pending_count == 3
    assert sink.batches == []

    recorder.record(EventCategory.INFO, "e3")
    sink.available = True
    await recorder.flush()

    assert calls == ["down", "up"]
    assert recorder.db_available
    assert sink.messages == ["e0", "e1", "e2", "e3"]
    assert recorder.pending_count == 0


async def test_full_buffer_drops_oldest_events() -> None:
    sink = FakeSink()
    sink.available = False
    recorder = EventRecorder(Environment.DEMO, sink=sink, max_buffer=3)
    for index in range(5):
        recorder.record(EventCategory.INFO, f"e{index}")
    await recorder.flush()

    assert recorder.pending_count == 3
    assert recorder.dropped_count == 2

    sink.available = True
    await recorder.flush()
    assert sink.messages == ["e2", "e3", "e4"]


async def test_rejected_event_does_not_block_others() -> None:
    sink = FakeSink()
    sink.reject_messages = {"bad"}
    recorder = EventRecorder(Environment.DEMO, sink=sink)
    for message in ("good1", "bad", "good2"):
        recorder.record(EventCategory.INFO, message)

    await recorder.flush()

    assert sink.messages == ["good1", "good2"]
    assert recorder.rejected_count == 1
    assert recorder.pending_count == 0


async def test_publish_failure_does_not_block_storage() -> None:
    sink, publisher = FakeSink(), FakePublisher()
    publisher.fail = True
    recorder = EventRecorder(Environment.DEMO, sink=sink, publisher=publisher)

    recorder.record(EventCategory.WARNING, "first")
    await recorder.flush()
    assert sink.messages == ["first"]
    assert publisher.frames == []

    publisher.fail = False
    recorder.record(EventCategory.WARNING, "second")
    await recorder.flush()
    assert [json.loads(frame)["payload"]["message"] for _, frame in publisher.frames] == [
        "second"
    ]


async def test_run_flushes_periodically() -> None:
    sink = FakeSink()
    recorder = EventRecorder(Environment.DEMO, sink=sink, flush_interval_s=0.01)
    task = asyncio.create_task(recorder.run())
    try:
        recorder.record(EventCategory.INFO, "tick")
        for _ in range(100):
            if sink.messages:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert sink.messages == ["tick"]


def test_refs_are_json_ready_and_timestamps_must_be_aware() -> None:
    recorder = EventRecorder(Environment.DEMO)
    marker = uuid.uuid4()

    event = recorder.record(
        EventCategory.TRADE,
        "filled",
        refs={"at": datetime(2026, 1, 1, tzinfo=UTC), "order": marker, "qty": Decimal("1.5")},
    )

    assert event.refs is not None
    assert event.refs["order"] == str(marker)
    assert isinstance(event.refs["at"], str)
    json.dumps(event.refs)
    with pytest.raises(ValidationError):
        recorder.record(EventCategory.INFO, "naive", ts=datetime(2026, 1, 1))


async def test_postgres_sink_reports_unreachable_database() -> None:
    engine = create_async_engine(
        "postgresql+asyncpg://mooo:mooo@127.0.0.1:1/mooo", poolclass=NullPool
    )
    try:
        sink = PostgresEventSink(async_sessionmaker(engine))
        event = EventMessage(
            environment=Environment.DEMO,
            category=EventCategory.INFO,
            message="x",
            ts=datetime.now(UTC),
        )
        with pytest.raises(DatabaseUnavailableError):
            await sink.write([event])
    finally:
        await engine.dispose()


async def test_postgres_sink_writes_redacted_rows() -> None:
    url = os.environ.get("MOOO_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail("MOOO_TEST_DATABASE_URL must be set in CI")
        pytest.skip("set MOOO_TEST_DATABASE_URL to a migrated database to run this test")
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                factory = async_sessionmaker(
                    bind=connection,
                    join_transaction_mode="create_savepoint",
                    expire_on_commit=False,
                )
                recorder = EventRecorder(Environment.DEMO, sink=PostgresEventSink(factory))
                marker = uuid.uuid4()
                recorder.record(
                    EventCategory.SECURITY,
                    "credential saved",
                    correlation_id=marker,
                    refs={"api_key": API_KEY, "masked_key": "****5678"},
                )
                recorder.record(EventCategory.INFO, "second", correlation_id=marker)
                await recorder.flush()

                assert recorder.pending_count == 0
                assert recorder.db_available
                async with factory() as session:
                    stmt = (
                        select(EventRecord)
                        .where(EventRecord.correlation_id == marker)
                        .order_by(EventRecord.id)
                    )
                    rows = list((await session.scalars(stmt)).all())
                assert [row.message for row in rows] == ["credential saved", "second"]
                assert rows[0].refs == {"api_key": REDACTED, "masked_key": "****5678"}
                assert rows[0].environment == Environment.DEMO
                assert rows[0].category == EventCategory.SECURITY
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
