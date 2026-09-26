import asyncio

import pytest

from mooo_core.inprocess_bus import (
    BusEvent,
    InProcessBus,
    KlineClosed,
    OrderBookUpdated,
    OrderEvent,
    PositionEvent,
    TickerUpdated,
)
from mooo_core.models import Environment

DEMO = Environment.DEMO


def _ticker(price: float) -> TickerUpdated:
    return TickerUpdated(environment=DEMO, symbol="BTCUSDT", data={"lastPrice": price})


def _order(link: str) -> OrderEvent:
    return OrderEvent(environment=DEMO, data={"orderLinkId": link})


async def test_subscribers_receive_only_their_types() -> None:
    bus = InProcessBus()
    tickers = bus.subscribe(TickerUpdated)
    account = bus.subscribe(OrderEvent, PositionEvent)
    everything = bus.subscribe(BusEvent)
    bus.publish(_ticker(1))
    bus.publish(_order("a"))
    bus.publish(PositionEvent(environment=DEMO, data={"size": "0.1"}))
    assert (len(tickers), len(account), len(everything)) == (1, 2, 3)
    assert isinstance(await account.get(), OrderEvent)


async def test_oldest_ticker_updates_are_dropped_under_backpressure() -> None:
    bus = InProcessBus()
    subscription = bus.subscribe(TickerUpdated, maxsize=3)
    for price in range(5):
        bus.publish(_ticker(price))
    assert len(subscription) == 3
    assert subscription.dropped == 2
    prices = []
    while (event := subscription.get_nowait()) is not None:
        prices.append(event.data["lastPrice"])
    assert prices == [2, 3, 4]


async def test_order_events_are_never_dropped() -> None:
    bus = InProcessBus()
    subscription = bus.subscribe(OrderEvent, TickerUpdated, maxsize=2)
    bus.publish(_ticker(1))
    bus.publish(_ticker(2))
    bus.publish(_order("a"))
    for index in range(5):
        bus.publish(_order(f"o{index}"))
    bus.publish(_ticker(3))

    items = []
    while (event := subscription.get_nowait()) is not None:
        items.append(event)
    orders = [item for item in items if isinstance(item, OrderEvent)]
    assert [order.data["orderLinkId"] for order in orders] == ["a", "o0", "o1", "o2", "o3", "o4"]
    assert not any(isinstance(item, TickerUpdated) for item in items)
    assert subscription.dropped == 3


async def test_get_waits_for_publish() -> None:
    bus = InProcessBus()
    subscription = bus.subscribe(KlineClosed)
    waiter = asyncio.create_task(subscription.get())
    await asyncio.sleep(0)
    assert not waiter.done()
    bus.publish(KlineClosed(environment=DEMO, symbol="ETHUSDT", interval="15", data={}))
    event = await asyncio.wait_for(waiter, timeout=1)
    assert event.interval == "15"


def test_unsubscribe_validation_and_drop_rules() -> None:
    bus = InProcessBus()
    subscription = bus.subscribe(TickerUpdated)
    subscription.close()
    assert bus.publish(_ticker(1)) == 0
    assert bus.subscriber_count == 0
    with pytest.raises(ValueError):
        bus.subscribe()
    with pytest.raises(ValueError):
        bus.subscribe(TickerUpdated, maxsize=0)
    assert TickerUpdated.DROPPABLE and OrderBookUpdated.DROPPABLE
    assert not OrderEvent.DROPPABLE
    assert not PositionEvent.DROPPABLE
