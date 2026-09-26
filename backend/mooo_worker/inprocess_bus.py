"""InProcessBus: typed asyncio pub/sub inside the Agent Worker (Event Bus and Observability).

Each subscriber has a bounded queue. Under backpressure the bus drops the oldest queued
``TickerUpdated`` for that subscriber, because a newer ticker supersedes it. Order,
execution, position, and all other event types are never dropped: when no ticker can be
evicted the queue grows past its bound and a warning is logged.

``publish`` is synchronous and never blocks, so a slow subscriber cannot stall the
WebSocket readers that publish market and account data.

The ``data`` payloads are refined by the market data, execution, regime, and news work.
"""

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Self

from mooo_core.models import utcnow

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 1_000


def _empty_data() -> Mapping[str, Any]:
    return {}


@dataclass(frozen=True, kw_only=True)
class BusEvent:
    """Base class for InProcessBus events."""

    droppable: ClassVar[bool] = False
    ts: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, kw_only=True)
class TickerUpdated(BusEvent):
    """Latest ticker for a symbol. The next update supersedes it, so it may be dropped."""

    droppable: ClassVar[bool] = True
    symbol: str
    data: Mapping[str, Any] = field(default_factory=_empty_data)


@dataclass(frozen=True, kw_only=True)
class KlineClosed(BusEvent):
    symbol: str
    interval: str
    data: Mapping[str, Any] = field(default_factory=_empty_data)


@dataclass(frozen=True, kw_only=True)
class OrderBookUpdated(BusEvent):
    symbol: str
    data: Mapping[str, Any] = field(default_factory=_empty_data)


@dataclass(frozen=True, kw_only=True)
class OrderEvent(BusEvent):
    symbol: str
    data: Mapping[str, Any] = field(default_factory=_empty_data)


@dataclass(frozen=True, kw_only=True)
class ExecutionEvent(BusEvent):
    symbol: str
    data: Mapping[str, Any] = field(default_factory=_empty_data)


@dataclass(frozen=True, kw_only=True)
class PositionEvent(BusEvent):
    symbol: str
    data: Mapping[str, Any] = field(default_factory=_empty_data)


@dataclass(frozen=True, kw_only=True)
class WalletEvent(BusEvent):
    data: Mapping[str, Any] = field(default_factory=_empty_data)


@dataclass(frozen=True, kw_only=True)
class RegimeChanged(BusEvent):
    symbol: str
    data: Mapping[str, Any] = field(default_factory=_empty_data)


@dataclass(frozen=True, kw_only=True)
class NewsAssessed(BusEvent):
    data: Mapping[str, Any] = field(default_factory=_empty_data)


class SubscriptionClosed(Exception):
    """Raised by ``Subscription.get`` after the subscription is closed and drained."""


class Subscription[E: BusEvent]:
    """One subscriber's bounded queue. Intended for a single consumer task."""

    def __init__(
        self,
        event_types: tuple[type[E], ...],
        maxsize: int,
        name: str,
        on_close: Callable[[Any], None],
    ) -> None:
        self.name = name
        self._event_types = event_types
        self._maxsize = maxsize
        self._queue: deque[E] = deque()
        self._ready = asyncio.Event()
        self._closed = False
        self._dropped = 0
        self._overflowing = False
        self._on_close = on_close

    @property
    def maxsize(self) -> int:
        return self._maxsize

    @property
    def qsize(self) -> int:
        return len(self._queue)

    @property
    def dropped(self) -> int:
        """Ticker updates dropped under backpressure."""
        return self._dropped

    @property
    def closed(self) -> bool:
        return self._closed

    def accepts(self, event: BusEvent) -> bool:
        return not self._closed and isinstance(event, self._event_types)

    def offer(self, event: E) -> None:
        """Enqueue without blocking, applying the backpressure rules."""
        if len(self._queue) >= self._maxsize:
            if self._evict_oldest_droppable():
                self._dropped += 1
            elif event.droppable:
                self._dropped += 1
                return
            elif not self._overflowing:
                self._overflowing = True
                logger.warning(
                    "Subscriber %s is over its queue bound (%d); critical events are kept",
                    self.name,
                    self._maxsize,
                )
        self._queue.append(event)
        self._ready.set()

    def _evict_oldest_droppable(self) -> bool:
        for index, queued in enumerate(self._queue):
            if queued.droppable:
                del self._queue[index]
                return True
        return False

    def get_nowait(self) -> E | None:
        return self._pop() if self._queue else None

    async def get(self) -> E:
        while not self._queue:
            if self._closed:
                raise SubscriptionClosed(self.name)
            self._ready.clear()
            await self._ready.wait()
        return self._pop()

    def _pop(self) -> E:
        event = self._queue.popleft()
        if self._overflowing and len(self._queue) < self._maxsize:
            self._overflowing = False
        return event

    def close(self) -> None:
        """Stop receiving new events. Queued events can still be read."""
        if self._closed:
            return
        self._closed = True
        self._ready.set()
        self._on_close(self)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> E:
        try:
            return await self.get()
        except SubscriptionClosed:
            raise StopAsyncIteration from None


class InProcessBus:
    """Typed pub/sub for events inside one Agent Worker process."""

    def __init__(self, *, default_maxsize: int = DEFAULT_QUEUE_SIZE) -> None:
        if default_maxsize < 1:
            raise ValueError("default_maxsize must be at least 1")
        self._default_maxsize = default_maxsize
        self._subscriptions: list[Subscription[Any]] = []

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    def subscribe[E: BusEvent](
        self,
        *event_types: type[E],
        maxsize: int | None = None,
        name: str = "subscriber",
    ) -> Subscription[E]:
        if not event_types:
            raise ValueError("subscribe needs at least one event type")
        size = self._default_maxsize if maxsize is None else maxsize
        if size < 1:
            raise ValueError("maxsize must be at least 1")
        subscription = Subscription(tuple(event_types), size, name, self._remove)
        self._subscriptions.append(subscription)
        return subscription

    def publish(self, event: BusEvent) -> int:
        """Deliver to every matching subscriber without blocking. Returns the count."""
        delivered = 0
        for subscription in tuple(self._subscriptions):
            if subscription.accepts(event):
                subscription.offer(event)
                delivered += 1
        return delivered

    def _remove(self, subscription: Subscription[Any]) -> None:
        with contextlib.suppress(ValueError):
            self._subscriptions.remove(subscription)
