import asyncio
import json
import os
import uuid
from collections.abc import Sequence

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from mooo_core.bus import EVENTS_CHANNEL
from mooo_core.events import EventRecorder, PendingEvent, SqlEventSink
from mooo_core.models import Environment, EventCategory, EventRecord
from mooo_core.redaction import REDACTED, SecretRedactor

SECRET = "live-secret-0123456789"


class MemorySink:
    def __init__(self) -> None:
        self.batches: list[list[PendingEvent]] = []
        self.fail = False

    async def write(self, events: Sequence[PendingEvent]) -> None:
        if self.fail:
            raise ConnectionError("database down")
        self.batches.append(list(events))

    @property
    def events(self) -> list[PendingEvent]:
        return [event for batch in self.batches for event in batch]


class MemoryPublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[tuple[str, str]] = []
        self.fail = fail

    async def publish(self, channel: str, message: str) -> int:
        if self.fail:
            raise ConnectionError("redis down")
        self.messages.append((channel, message))
        return 1


def _redactor() -> SecretRedactor:
    active = SecretRedactor()
    active.register(SECRET)
    return active


async def test_events_are_redacted_before_write_and_publish() -> None:
    sink, publisher = MemorySink(), MemoryPublisher()
    recorder = EventRecorder(sink, publisher, redactor=_redactor())
    correlation_id = uuid.uuid4()
    await recorder.record(
        Environment.DEMO,
        EventCategory.SECURITY,
        f"key check failed for {SECRET}",
        correlation_id=correlation_id,
        refs={"api_secret": "raw", "masked_key": "****6789", "detail": f"x {SECRET}"},
    )
    assert await recorder.flush() == 1

    stored = sink.events[0]
    assert SECRET not in stored.message
    assert REDACTED in stored.message
    expected_refs = {"api_secret": REDACTED, "masked_key": "****6789", "detail": f"x {REDACTED}"}
    assert stored.refs == expected_refs
    assert stored.correlation_id == correlation_id

    channel, message = publisher.messages[0]
    assert channel == EVENTS_CHANNEL
    assert SECRET not in message
    frame = json.loads(message)
    assert frame["type"] == "event"
    assert frame["environment"] == "demo"
    assert frame["payload"]["correlation_id"] == str(correlation_id)


async def test_events_of_one_proposal_share_correlation_id_in_order() -> None:
    sink = MemorySink()
    recorder = EventRecorder(sink)
    correlation_id = uuid.uuid4()
    steps = ["proposal", "risk verdict", "order submitted"]
    for message in steps:
        await recorder.record(
            Environment.DEMO, EventCategory.STRATEGY, message, correlation_id=correlation_id
        )
    await recorder.flush()
    assert [event.message for event in sink.events] == steps
    assert {event.correlation_id for event in sink.events} == {correlation_id}


async def test_database_outage_buffers_events_and_reports_status() -> None:
    statuses: list[bool] = []

    async def on_status(available: bool) -> None:
        statuses.append(available)

    sink = MemorySink()
    sink.fail = True
    recorder = EventRecorder(sink, on_database_status=on_status, batch_size=2)
    for index in range(5):
        await recorder.record(Environment.DEMO, EventCategory.INFO, f"e{index}")

    assert await recorder.flush() == 0
    assert recorder.pending == 5
    assert recorder.database_available is False
    assert await recorder.flush() == 0
    assert statuses == [False]

    sink.fail = False
    assert await recorder.flush() == 5
    assert [event.message for event in sink.events] == [f"e{index}" for index in range(5)]
    assert statuses == [False, True]
    assert recorder.pending == 0


async def test_buffer_limit_keeps_the_newest_events() -> None:
    sink = MemorySink()
    sink.fail = True
    recorder = EventRecorder(sink, buffer_limit=3)
    for index in range(5):
        await recorder.record(Environment.DEMO, EventCategory.INFO, f"e{index}")
    await recorder.flush()
    assert recorder.pending == 3
    assert recorder.dropped == 2

    sink.fail = False
    await recorder.flush()
    assert [event.message for event in sink.events] == ["e2", "e3", "e4"]
    assert EventRecorder(MemorySink()).buffer_limit == 10_000


async def test_publish_failure_does_not_block_recording() -> None:
    sink = MemorySink()
    recorder = EventRecorder(sink, MemoryPublisher(fail=True))
    await recorder.record(Environment.DEMO, EventCategory.WARNING, "redis is down")
    assert await recorder.flush() == 1


async def test_run_flushes_until_stopped() -> None:
    sink = MemorySink()
    recorder = EventRecorder(sink, flush_interval_s=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(recorder.run(stop))
    await recorder.record(Environment.DEMO, EventCategory.INFO, "first")
    await asyncio.sleep(0.1)
    assert [event.message for event in sink.events] == ["first"]

    await recorder.record(Environment.DEMO, EventCategory.INFO, "last")
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert [event.message for event in sink.events] == ["first", "last"]


async def test_sql_sink_writes_redacted_rows() -> None:
    url = os.environ.get("MOOO_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail("MOOO_TEST_DATABASE_URL must be set in CI")
        pytest.skip("set MOOO_TEST_DATABASE_URL to a migrated database to run this test")
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        recorder = EventRecorder(SqlEventSink(async_sessionmaker(engine, expire_on_commit=False)))
        correlation_id = uuid.uuid4()
        await recorder.record(
            Environment.DEMO,
            EventCategory.RISK,
            "verdict",
            symbol="BTCUSDT",
            correlation_id=correlation_id,
            refs={"token": "abc", "rule": "max_leverage"},
        )
        await recorder.record(
            Environment.DEMO, EventCategory.EXECUTION, "order", correlation_id=correlation_id
        )
        assert await recorder.flush() == 2

        async with engine.connect() as connection:
            result = await connection.execute(
                sa.select(EventRecord.message, EventRecord.refs)
                .where(EventRecord.correlation_id == correlation_id)
                .order_by(EventRecord.id)
            )
            rows = result.all()
        assert [row.message for row in rows] == ["verdict", "order"]
        assert rows[0].refs == {"token": REDACTED, "rule": "max_leverage"}
    finally:
        await engine.dispose()
