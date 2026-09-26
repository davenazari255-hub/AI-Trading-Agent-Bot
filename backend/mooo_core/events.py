"""EventRecorder: categorized, correlated, redacted audit Events (Event Bus and Observability).

``record()`` is synchronous and never blocks. Every ``flush_interval_s`` (250 ms) the
recorder publishes new Events to the Redis ``mooo:events`` channel as ``StreamMessage``
frames and writes pending Events to PostgreSQL in batches. When PostgreSQL is unavailable
it keeps up to ``max_buffer`` (10,000) Events in memory, reports the outage once through
``on_db_unavailable`` (the Kill Switch hook), and retries on every flush. The oldest Events
are dropped only when the buffer is full.

The message, symbol, and refs of every Event pass through the ``SecretRedactor`` before
the Event is buffered, so no credential or session token reaches PostgreSQL or Redis.
"""

import asyncio
import inspect
import logging
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

import sqlalchemy as sa
from pydantic_core import to_jsonable_python
from redis.exceptions import RedisError
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mooo_core.bus import EVENTS_CHANNEL
from mooo_core.log import coerce_correlation_id, current_correlation_id
from mooo_core.models import Environment, EventCategory, EventRecord, utcnow
from mooo_core.redaction import SecretRedactor, get_redactor
from mooo_core.stream import EventMessage, StreamMessage

logger = logging.getLogger(__name__)

DEFAULT_FLUSH_INTERVAL_S = 0.25
DEFAULT_MAX_BUFFER = 10_000
DEFAULT_MAX_BATCH = 500

StatusCallback = Callable[[], Awaitable[None] | None]


class DatabaseUnavailableError(Exception):
    """PostgreSQL could not be reached. The batch stays buffered and is retried."""


class EventSink(Protocol):
    """Durable store for Events. Raises DatabaseUnavailableError when it cannot be reached."""

    async def write(self, events: Sequence[EventMessage]) -> None: ...


class EventPublisher(Protocol):
    """Anything with a Redis-style ``publish(channel, message)``."""

    def publish(self, channel: str, message: str, /) -> Any: ...


_CONNECTIVITY_ERRORS: tuple[type[BaseException], ...] = (
    sa_exc.OperationalError,
    sa_exc.InterfaceError,
    sa_exc.DisconnectionError,
    sa_exc.TimeoutError,
    OSError,
    TimeoutError,
)


def _row(event: EventMessage) -> dict[str, Any]:
    return {
        "environment": event.environment,
        "category": event.category,
        "message": event.message,
        "symbol": event.symbol,
        "correlation_id": event.correlation_id,
        "refs": event.refs,
        "ts": event.ts,
    }


class PostgresEventSink:
    """Writes a batch of Events to the partitioned ``events`` table in one transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def write(self, events: Sequence[EventMessage]) -> None:
        if not events:
            return
        rows = [_row(event) for event in events]
        try:
            async with self._session_factory() as session:
                await session.execute(sa.insert(EventRecord), rows)
                await session.commit()
        except _CONNECTIVITY_ERRORS as exc:
            raise DatabaseUnavailableError(type(exc).__name__) from exc
        except sa_exc.DBAPIError as exc:
            if exc.connection_invalidated:
                raise DatabaseUnavailableError(type(exc).__name__) from exc
            raise


async def _notify(callback: StatusCallback | None) -> None:
    if callback is None:
        return
    try:
        result = callback()
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.exception("Database status callback failed")


class EventRecorder:
    """Buffers, redacts, persists, and publishes Events for one process."""

    def __init__(
        self,
        environment: Environment | str,
        *,
        sink: EventSink | None = None,
        publisher: EventPublisher | None = None,
        redactor: SecretRedactor | None = None,
        on_db_unavailable: StatusCallback | None = None,
        on_db_recovered: StatusCallback | None = None,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        max_buffer: int = DEFAULT_MAX_BUFFER,
        max_batch: int = DEFAULT_MAX_BATCH,
        channel: str = EVENTS_CHANNEL,
    ) -> None:
        if flush_interval_s <= 0:
            raise ValueError("flush_interval_s must be positive")
        if max_buffer < 1 or max_batch < 1:
            raise ValueError("max_buffer and max_batch must be at least 1")
        self._environment = Environment(environment)
        self._sink = sink
        self._publisher = publisher
        self._redactor = redactor if redactor is not None else get_redactor()
        self._on_db_unavailable = on_db_unavailable
        self._on_db_recovered = on_db_recovered
        self._flush_interval_s = flush_interval_s
        self._max_buffer = max_buffer
        self._max_batch = max_batch
        self._channel = channel
        self._pending: deque[EventMessage] = deque()
        self._to_publish: deque[EventMessage] = deque()
        self._flush_lock = asyncio.Lock()
        self._db_available = True
        self._publish_failing = False
        self._dropped = 0
        self._rejected = 0

    @property
    def environment(self) -> Environment:
        return self._environment

    @property
    def flush_interval_s(self) -> float:
        return self._flush_interval_s

    @property
    def max_buffer(self) -> int:
        return self._max_buffer

    @property
    def pending_count(self) -> int:
        """Events waiting to be written to PostgreSQL."""
        return len(self._pending)

    @property
    def dropped_count(self) -> int:
        """Events dropped because the memory buffer was full."""
        return self._dropped

    @property
    def rejected_count(self) -> int:
        """Events the database refused (for example invalid data) and that were dropped."""
        return self._rejected

    @property
    def db_available(self) -> bool:
        return self._db_available

    def record(
        self,
        category: EventCategory | str,
        message: str,
        *,
        symbol: str | None = None,
        correlation_id: uuid.UUID | str | None = None,
        refs: Mapping[str, Any] | None = None,
        environment: Environment | str | None = None,
        ts: datetime | None = None,
    ) -> EventMessage:
        """Redact and buffer one Event. Never blocks and never touches the network.

        Without an explicit ``correlation_id`` the Event takes the Correlation Identifier
        bound by ``correlation_scope``, so all Events of one proposal share it.
        """
        resolved_correlation_id = (
            coerce_correlation_id(correlation_id)
            if correlation_id is not None
            else current_correlation_id()
        )
        event = EventMessage(
            environment=(
                Environment(environment) if environment is not None else self._environment
            ),
            category=EventCategory(category),
            message=self._redactor.redact_text(message),
            symbol=self._redactor.redact_text(symbol) if symbol is not None else None,
            correlation_id=resolved_correlation_id,
            refs=self._redact_refs(refs),
            ts=ts if ts is not None else utcnow(),
        )
        if self._sink is not None:
            if len(self._pending) >= self._max_buffer:
                self._pending.popleft()
                self._count_dropped(1)
            self._pending.append(event)
        if self._publisher is not None:
            if len(self._to_publish) >= self._max_buffer:
                self._to_publish.popleft()
            self._to_publish.append(event)
        return event

    def _redact_refs(self, refs: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if refs is None:
            return None
        redacted = self._redactor.redact_mapping(refs)
        jsonable = to_jsonable_python(redacted, fallback=str)
        if not isinstance(jsonable, dict):
            return {"value": jsonable}
        return jsonable

    def _count_dropped(self, count: int) -> None:
        before = self._dropped
        self._dropped += count
        if before == 0 or self._dropped // 1000 > before // 1000:
            logger.warning(
                "Event buffer is full (%d events); %d oldest events dropped so far",
                self._max_buffer,
                self._dropped,
            )

    async def flush(self) -> None:
        """Publish new Events to Redis, then write pending Events to PostgreSQL."""
        async with self._flush_lock:
            await self._publish_pending()
            await self._write_pending()

    async def run(self) -> None:
        """Flush every ``flush_interval_s`` until cancelled. Suitable for TaskSupervisor."""
        while True:
            await asyncio.sleep(self._flush_interval_s)
            try:
                await self.flush()
            except Exception:
                logger.exception("Event flush failed")

    async def aclose(self) -> None:
        """Final flush on shutdown."""
        await self.flush()

    async def _publish_pending(self) -> None:
        publisher = self._publisher
        if publisher is None:
            return
        while self._to_publish:
            event = self._to_publish.popleft()
            frame = StreamMessage.for_event(event).model_dump_json()
            try:
                result = publisher.publish(self._channel, frame)
                if inspect.isawaitable(result):
                    await result
            except (RedisError, OSError, TimeoutError) as exc:
                # Pub/sub is not durable: the dashboard refetches snapshots after reconnect.
                self._to_publish.clear()
                if not self._publish_failing:
                    self._publish_failing = True
                    logger.warning(
                        "Could not publish events to %s (%s)", self._channel, type(exc).__name__
                    )
                return
            if self._publish_failing:
                self._publish_failing = False
                logger.info("Publishing events to %s recovered", self._channel)

    async def _write_pending(self) -> None:
        sink = self._sink
        if sink is None:
            return
        while self._pending:
            size = min(self._max_batch, len(self._pending))
            batch = [self._pending.popleft() for _ in range(size)]
            try:
                await sink.write(batch)
            except DatabaseUnavailableError:
                self._restore(batch)
                await self._set_db_available(False)
                return
            except asyncio.CancelledError:
                self._restore(batch)
                raise
            except Exception as exc:
                if not await self._write_one_by_one(sink, batch, exc):
                    return
                continue
            await self._set_db_available(True)

    async def _write_one_by_one(
        self, sink: EventSink, batch: Sequence[EventMessage], batch_error: Exception
    ) -> bool:
        """Isolate Events the database rejects. Returns False when the database went away."""
        logger.error(
            "Event batch rejected (%s); retrying %d events one by one",
            type(batch_error).__name__,
            len(batch),
        )
        for index, event in enumerate(batch):
            try:
                await sink.write([event])
            except DatabaseUnavailableError:
                self._restore(batch[index:])
                await self._set_db_available(False)
                return False
            except asyncio.CancelledError:
                self._restore(batch[index:])
                raise
            except Exception as exc:
                self._rejected += 1
                logger.error(
                    "Event dropped because the database rejected it (%s, category=%s)",
                    type(exc).__name__,
                    event.category.value,
                )
        await self._set_db_available(True)
        return True

    def _restore(self, batch: Sequence[EventMessage]) -> None:
        self._pending.extendleft(reversed(batch))
        overflow = len(self._pending) - self._max_buffer
        if overflow > 0:
            for _ in range(overflow):
                self._pending.popleft()
            self._count_dropped(overflow)

    async def _set_db_available(self, available: bool) -> None:
        if available == self._db_available:
            return
        self._db_available = available
        if available:
            logger.info(
                "PostgreSQL is available again; writing %d buffered events", len(self._pending)
            )
            await _notify(self._on_db_recovered)
        else:
            logger.error(
                "PostgreSQL is unavailable; buffering up to %d events in memory (%d pending)",
                self._max_buffer,
                len(self._pending),
            )
            await _notify(self._on_db_unavailable)
