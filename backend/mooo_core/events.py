"""EventRecorder: batched, redacted Event Log writes plus Redis publishing.

Events are redacted, buffered in memory, published to Redis ``mooo:events`` right away,
and written to PostgreSQL in batches (every 250 ms when ``run`` is used). When PostgreSQL
is down, up to 10,000 events stay buffered and the database status callback is told, so
the Kill Switch can react.
"""

import asyncio
import inspect
import json
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mooo_core.bus import EVENTS_CHANNEL
from mooo_core.models import Environment, EventCategory, EventRecord, utcnow
from mooo_core.redaction import SecretRedactor, default_redactor

DEFAULT_FLUSH_INTERVAL_S = 0.25
DEFAULT_BUFFER_LIMIT = 10_000
DEFAULT_BATCH_SIZE = 500

logger = structlog.get_logger(__name__)

DatabaseStatusCallback = Callable[[bool], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PendingEvent:
    environment: Environment
    category: EventCategory
    message: str
    ts: datetime
    symbol: str | None = None
    correlation_id: uuid.UUID | None = None
    refs: dict[str, Any] | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "environment": self.environment,
            "category": self.category,
            "message": self.message,
            "symbol": self.symbol,
            "correlation_id": self.correlation_id,
            "refs": self.refs,
        }

    def to_stream_message(self) -> dict[str, Any]:
        """``StreamMessage`` frame: ``{type, environment, ts, payload}``."""
        return {
            "type": "event",
            "environment": self.environment.value,
            "ts": self.ts.isoformat(),
            "payload": {
                "category": self.category.value,
                "message": self.message,
                "symbol": self.symbol,
                "correlation_id": str(self.correlation_id) if self.correlation_id else None,
                "refs": self.refs,
            },
        }


class EventSink(Protocol):
    async def write(self, events: Sequence[PendingEvent]) -> None: ...


class EventPublisher(Protocol):
    def publish(self, channel: str, message: str) -> Any: ...


class SqlEventSink:
    """Writes batches of events to the partitioned ``events`` table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def write(self, events: Sequence[PendingEvent]) -> None:
        if not events:
            return
        async with self._session_factory() as session:
            await session.execute(sa.insert(EventRecord), [event.to_row() for event in events])
            await session.commit()


class EventRecorder:
    def __init__(
        self,
        sink: EventSink,
        publisher: EventPublisher | None = None,
        *,
        redactor: SecretRedactor | None = None,
        channel: str = EVENTS_CHANNEL,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        buffer_limit: int = DEFAULT_BUFFER_LIMIT,
        batch_size: int = DEFAULT_BATCH_SIZE,
        on_database_status: DatabaseStatusCallback | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if buffer_limit < 1 or batch_size < 1 or flush_interval_s <= 0:
            raise ValueError("buffer_limit, batch_size, and flush_interval_s must be positive")
        self._sink = sink
        self._publisher = publisher
        self._redactor = redactor or default_redactor
        self._channel = channel
        self._flush_interval_s = flush_interval_s
        self._buffer_limit = buffer_limit
        self._batch_size = batch_size
        self._on_database_status = on_database_status
        self._clock = clock
        self._buffer: deque[PendingEvent] = deque()
        self._dropped = 0
        self._database_available = True
        self._flush_lock = asyncio.Lock()

    @property
    def pending(self) -> int:
        return len(self._buffer)

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def buffer_limit(self) -> int:
        return self._buffer_limit

    @property
    def database_available(self) -> bool:
        return self._database_available

    async def record(
        self,
        environment: Environment,
        category: EventCategory,
        message: str,
        *,
        symbol: str | None = None,
        correlation_id: uuid.UUID | None = None,
        refs: Mapping[str, Any] | None = None,
    ) -> PendingEvent:
        event = PendingEvent(
            environment=environment,
            category=category,
            message=self._redactor.redact_text(message),
            ts=self._clock(),
            symbol=symbol,
            correlation_id=correlation_id,
            refs=self._redactor.redact(dict(refs)) if refs is not None else None,
        )
        self._buffer.append(event)
        if len(self._buffer) > self._buffer_limit:
            self._buffer.popleft()
            self._dropped += 1
        await self._publish(event)
        return event

    async def flush(self) -> int:
        """Write buffered events in batches. Return the number written."""
        written = 0
        async with self._flush_lock:
            while self._buffer:
                count = min(self._batch_size, len(self._buffer))
                batch = [self._buffer.popleft() for _ in range(count)]
                try:
                    await self._sink.write(batch)
                except Exception as exc:
                    self._requeue(batch)
                    await self._set_database_available(False, exc)
                    break
                except BaseException:
                    self._requeue(batch)
                    raise
                written += len(batch)
                await self._set_database_available(True)
        return written

    async def run(self, stop: asyncio.Event) -> None:
        """Flush every ``flush_interval_s`` until ``stop`` is set, then flush once more."""
        while not stop.is_set():
            await self.flush()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._flush_interval_s)
            except TimeoutError:
                pass
        await self.flush()

    def _requeue(self, batch: list[PendingEvent]) -> None:
        combined = [*batch, *self._buffer]
        overflow = len(combined) - self._buffer_limit
        if overflow > 0:
            self._dropped += overflow
            combined = combined[overflow:]
        self._buffer = deque(combined)

    async def _set_database_available(
        self, available: bool, error: BaseException | None = None
    ) -> None:
        if available == self._database_available:
            return
        self._database_available = available
        if available:
            logger.info("event_log_database_recovered", pending=len(self._buffer))
        else:
            logger.error(
                "event_log_database_unavailable",
                error=type(error).__name__ if error else None,
                pending=len(self._buffer),
            )
        if self._on_database_status is None:
            return
        try:
            await self._on_database_status(available)
        except Exception as exc:
            logger.error("database_status_callback_failed", error=type(exc).__name__)

    async def _publish(self, event: PendingEvent) -> None:
        if self._publisher is None:
            return
        try:
            message = json.dumps(event.to_stream_message(), default=str)
            result = self._publisher.publish(self._channel, message)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning("event_publish_failed", error=type(exc).__name__)
