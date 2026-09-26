"""Client-side rate limiting for Bybit V5 REST (Exchange Adapter blueprint).

A token bucket per endpoint group keeps request rates under Bybit's documented limits.
When Bybit reports that a limit is used up, or answers with a rate-limit error, the
group is blocked until the reset time Bybit reports (X-Bapi-Limit-Reset-Timestamp).
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]

LIMIT_STATUS_HEADER = "x-bapi-limit-status"
LIMIT_RESET_HEADER = "x-bapi-limit-reset-timestamp"


class EndpointGroup(StrEnum):
    MARKET = "market"
    TRADE = "trade"
    ORDER_QUERY = "order_query"
    POSITION = "position"
    ACCOUNT = "account"


# (requests per second, burst). Each rate is below Bybit's documented default for the
# group: market data 600 per 5 s per IP; linear order create, amend, and cancel 10/s per
# UID; order, execution, position, and account queries 50/s per UID.
DEFAULT_RATES: Mapping[EndpointGroup, tuple[float, int]] = {
    EndpointGroup.MARKET: (20.0, 20),
    EndpointGroup.TRADE: (8.0, 8),
    EndpointGroup.ORDER_QUERY: (10.0, 10),
    EndpointGroup.POSITION: (8.0, 8),
    EndpointGroup.ACCOUNT: (5.0, 5),
}

_TRADE_PATHS = frozenset(
    {"/v5/order/create", "/v5/order/amend", "/v5/order/cancel", "/v5/order/cancel-all"}
)


def endpoint_group(path: str) -> EndpointGroup:
    """Return the rate-limit group of an allow-listed path."""
    if path.startswith("/v5/market/"):
        return EndpointGroup.MARKET
    if path in _TRADE_PATHS:
        return EndpointGroup.TRADE
    if path.startswith(("/v5/order/", "/v5/execution/")):
        return EndpointGroup.ORDER_QUERY
    if path.startswith("/v5/position/"):
        return EndpointGroup.POSITION
    return EndpointGroup.ACCOUNT


def parse_reset_timestamp(value: str | None) -> float | None:
    """Convert X-Bapi-Limit-Reset-Timestamp (epoch milliseconds) to epoch seconds."""
    if value is None:
        return None
    try:
        millis = int(value.strip())
    except ValueError:
        return None
    return millis / 1000.0 if millis > 0 else None


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class TokenBucket:
    """Token bucket with a refill rate in tokens per second and a burst capacity."""

    def __init__(self, rate_per_s: float, capacity: int, *, clock: Clock = time.time) -> None:
        if rate_per_s <= 0 or capacity < 1:
            raise ValueError("rate_per_s must be positive and capacity at least 1")
        self.rate_per_s = rate_per_s
        self.capacity = float(capacity)
        self._clock = clock
        self._tokens = float(capacity)
        self._updated = clock()

    def reserve(self) -> float:
        """Take one token and return the seconds to wait before it may be used."""
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_s)
        self._updated = now
        self._tokens -= 1.0
        if self._tokens >= 0:
            return 0.0
        return -self._tokens / self.rate_per_s


class RateLimiter:
    """Per-group token buckets plus blocks until Bybit's reported reset time."""

    def __init__(
        self,
        rates: Mapping[EndpointGroup, tuple[float, int]] | None = None,
        *,
        clock: Clock = time.time,
        sleep: Sleep = _default_sleep,
    ) -> None:
        merged = {**DEFAULT_RATES, **(rates or {})}
        self._buckets = {
            group: TokenBucket(rate, burst, clock=clock) for group, (rate, burst) in merged.items()
        }
        self._blocked_until: dict[EndpointGroup, float] = {}
        self._clock = clock
        self._sleep = sleep

    async def acquire(self, group: EndpointGroup) -> float:
        """Wait until one request in ``group`` may be sent. Returns the seconds waited."""
        wait = self._buckets[group].reserve()
        blocked = self._blocked_until.get(group)
        if blocked is not None:
            wait = max(wait, blocked - self._clock())
        if wait > 0:
            await self._sleep(wait)
            return wait
        return 0.0

    def block_until(self, group: EndpointGroup, until: float) -> None:
        self._blocked_until[group] = max(self._blocked_until.get(group, 0.0), until)

    def block_all_until(self, until: float) -> None:
        for group in self._buckets:
            self.block_until(group, until)

    def blocked_until(self, group: EndpointGroup) -> float | None:
        until = self._blocked_until.get(group)
        if until is None or until <= self._clock():
            return None
        return until

    def update_from_headers(
        self, group: EndpointGroup, headers: Mapping[str, str]
    ) -> float | None:
        """Read Bybit limit headers. Blocks the group when no requests remain.

        Returns the reported reset time in epoch seconds, or None.
        """
        lowered = {key.lower(): value for key, value in headers.items()}
        reset_at = parse_reset_timestamp(lowered.get(LIMIT_RESET_HEADER))
        remaining = _parse_int(lowered.get(LIMIT_STATUS_HEADER))
        if reset_at is not None and remaining is not None and remaining <= 0:
            self.block_until(group, reset_at)
        return reset_at
