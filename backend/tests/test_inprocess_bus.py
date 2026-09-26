"""InProcessBus: typed delivery and backpressure rules."""

import asyncio
import dataclasses

import pytest

from mooo_worker.inprocess_bus import (
    InProcessBus,
    KlineClosed,
    OrderEvent,
    PositionEvent,
    SubscriptionClosed,
    TickerUpdated,
)


async def test_subscribers_receive_only_their_types() -> None:
    bus = InProcessBus()
    tickers = bus.subscribe(TickerUpdated, name="tickers")
    orders = bus.subscribe(OrderEvent, PositionEvent, name="orders")

    assert bus.publish(TickerUpdated(symbol="BTCUSDT", data={"last": 1})) == 1
    bus.publish(OrderEvent(symbol="BTCUSDT", data={"status": "New"}))
    bus.publish(PositionEvent(symbol="BTCUSDT"))

    assert (await tickers.get()).symbol == "BTCUSDT"
    assert tickers.qsize == 0
    assert isinstance(await orders.get(), OrderEvent)
    assert isinstance(await orders.get(), PositionEvent)


def test_backpressure_drops_oldest_ticker_updates() -> None:
    bus = InProcessBus()
    sub = bus.subscribe(TickerUpdated, OrderEvent, maxsize=3)

    bus.publish(TickerUpdated(symbol="BTCUSDT", data={"n": 1}))
    bus.publish(OrderEvent(symbol="BTCUSDT", data={"n": "o1"}))
    bus.publish(TickerUpdated(symbol="BTCUSDT", data={"n": 2}))
    bus.publish(TickerUpdated(symbol="BTCUSDT", data={"n": 3}))
    bus.publish(TickerUpdated(symbol="BTCUSDT", data={"n": 4}))

    drained = []
    while (event := sub.get_nowait()) is not None:
        drained.append(event.data["n"])
    assert drained == ["o1", 3, 4]
    assert sub.dropped == 2


def test_order_and_position_events_are_never_dropped() -> None:
    bus = InProcessBus()
    sub = bus.subscribe(OrderEvent, PositionEvent, maxsize=2)

    for index in range(5):
        bus.publish(OrderEvent(symbol="BTCUSDT", data={"n": index}))
    bus.publish(PositionEvent(symbol="BTCUSDT"))

    assert sub.qsize == 6
    assert sub.dropped == 0


def test_ticker_is_dropped_when_queue_holds_only_critical_events() -> None:
    bus = InProcessBus()
    sub = bus.subscribe(TickerUpdated, OrderEvent, maxsize=1)

    bus.publish(OrderEvent(symbol="BTCUSDT"))
    bus.publish(TickerUpdated(symbol="BTCUSDT"))

    assert sub.qsize == 1
    assert sub.dropped == 1
    assert isinstance(sub.get_nowait(), OrderEvent)


async def test_iteration_ends_after_close() -> None:
    bus = InProcessBus()
    sub = bus.subscribe(KlineClosed, name="klines")
    received: list[str] = []

    async def consume() -> None:
        async for event in sub:
            received.append(event.interval)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    bus.publish(KlineClosed(symbol="BTCUSDT", interval="1"))
    bus.publish(KlineClosed(symbol="BTCUSDT", interval="5"))
    await asyncio.sleep(0)
    sub.close()
    await asyncio.wait_for(task, timeout=1)

    assert received == ["1", "5"]
    assert bus.publish(KlineClosed(symbol="BTCUSDT", interval="15")) == 0
    assert bus.subscriber_count == 0
    with pytest.raises(SubscriptionClosed):
        await sub.get()


def test_validation_and_frozen_events() -> None:
    bus = InProcessBus()
    with pytest.raises(ValueError):
        bus.subscribe()
    with pytest.raises(ValueError):
        bus.subscribe(OrderEvent, maxsize=0)
    event = OrderEvent(symbol="BTCUSDT")
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.symbol = "ETHUSDT"  # type: ignore[misc]
