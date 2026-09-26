"""InProcessBus: typed asyncio pub/sub inside the Agent Worker.

Each subscriber has a bounded queue. Under backpressure the oldest droppable event
(ticker or order book update) is dropped first. Order, execution, position, and wallet
events are never dropped; the queue grows past its bound instead.
"""

import asyncio
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from mooo_core.models import Environment, utcnow

DEFAULT_SUBSCRIBER_QUEUE = 1_000


@dataclass(frozen=True, slots=True, kw_only=True)
class BusEvent:
    DROPPABLE: ClassVar[bool] = False

    environment: Environment
    ts: datetime = field(default_factory=utcnow)
    correlation_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TickerUpdated(BusEvent):
    DROPPABLE: ClassVar[bool] = True

    symbol: str
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderBookUpdated(BusEvent):
    DROPPABLE: ClassVar[bool] = True

    symbol: str
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class KlineClosed(BusEvent):
    symbol: str
    interval: str
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderEvent(BusEvent):
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionEvent(BusEvent):
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class PositionEvent(BusEvent):
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class WalletEvent(BusEvent):
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class RegimeChanged(BusEvent):
    symbol: str
    from_regime: str | None
    to_regime: str


@dataclass(frozen=True, slots=True, kw_only=True)
class NewsAssessed(BusEvent):
    news_item_id: uuid.UUID
    data: Mapping[str, Any]


class Subscription[E: BusEvent]:
    def __init__(
        self, bus: "InProcessBus", types: tuple[type[E], ...], maxsize: int, name: str
    ) -> None:
        self._bus = bus
        self.types = types
        self.maxsize = maxsize
        self.name = name
        self.dropped = 0
        self._items: deque[E] = deque()
        self._ready = asyncio.Event()

    def __len__(self) -> int:
        return len(self._items)

    def offer(self, event: E) -> None:
        if len(self._items) >= self.maxsize:
            index = next((i for i, item in enumerate(self._items) if item.DROPPABLE), None)
            if index is not None:
                del self._items[index]
                self.dropped += 1
            elif event.DROPPABLE:
                self.dropped += 1
                return
        self._items.append(event)
        self._ready.set()

    def get_nowait(self) -> E | None:
        return self._items.popleft() if self._items else None

    async def get(self) -> E:
        while not self._items:
            self._ready.clear()
            await self._ready.wait()
        return self._items.popleft()

    def close(self) -> None:
        self._bus.unsubscribe(self)


class InProcessBus:
    def __init__(self) -> None:
        self._subscriptions: list[Subscription[Any]] = []

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    def subscribe[E: BusEvent](
        self, *types: type[E], maxsize: int = DEFAULT_SUBSCRIBER_QUEUE, name: str = ""
    ) -> Subscription[E]:
        if not types:
            raise ValueError("subscribe needs at least one event type")
        if maxsize < 1:
            raise ValueError("maxsize must be at least 1")
        subscription = Subscription(self, types, maxsize, name)
        self._subscriptions.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription[Any]) -> None:
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)

    def publish(self, event: BusEvent) -> int:
        """Deliver ``event`` to matching subscribers without blocking. Return the count."""
        delivered = 0
        for subscription in list(self._subscriptions):
            if isinstance(event, subscription.types):
                subscription.offer(event)
                delivered += 1
        return delivered
