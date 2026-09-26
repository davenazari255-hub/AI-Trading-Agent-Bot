"""Token-bucket rate limiting for Bybit REST calls (Exchange Adapter blueprint).

Each endpoint group has one bucket, and one global bucket covers every request. A request
waits until both its group bucket and the global bucket have a token. When Bybit reports a
limit (remaining quota 0 or a rate-limit error), the group, or every bucket for IP-wide
limits, is blocked until the reset time Bybit reports.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping

from mooo_core.exchange.endpoints import (
    DEFAULT_GROUP_LIMITS,
    GLOBAL_LIMIT,
    EndpointGroup,
    GroupLimit,
)

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]

_EPSILON = 1e-9


class TokenBucket:
    """Classic token bucket on a monotonic clock."""

    def __init__(self, rate_per_s: float, burst: float, *, clock: Clock = time.monotonic) -> None:
        if rate_per_s <= 0:
            raise ValueError("rate_per_s must be positive")
        if burst < 1:
            raise ValueError("burst must be at least 1")
        self.rate_per_s = rate_per_s
        self.burst = burst
        self._clock = clock
        self._tokens = burst
        # Tokens accrue from this instant. It is in the future while the bucket is blocked.
        self._updated = clock()

    def _refill(self) -> float:
        now = self._clock()
        if now > self._updated:
            self._tokens = min(self.burst, self._tokens + (now - self._updated) * self.rate_per_s)
            self._updated = now
        return now

    def wait_time(self, tokens: float = 1.0) -> float:
        """Seconds until ``tokens`` are available. Does not consume."""
        now = self._refill()
        blocked = max(0.0, self._updated - now)
        deficit = max(0.0, tokens - self._tokens) / self.rate_per_s
        return blocked + deficit

    def consume(self, tokens: float = 1.0) -> None:
        self._refill()
        self._tokens -= tokens

    def block_for(self, seconds: float) -> None:
        """Allow no request for ``seconds``; one request is allowed at the end of the block."""
        if seconds <= 0:
            return
        now = self._refill()
        until = now + seconds
        if until > self._updated:
            self._updated = until
            self._tokens = min(self.burst, 1.0)


class RateLimiter:
    """Per endpoint group token buckets plus one global bucket."""

    def __init__(
        self,
        group_limits: Mapping[EndpointGroup, GroupLimit] | None = None,
        *,
        global_limit: GroupLimit | None = GLOBAL_LIMIT,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        limits = {**DEFAULT_GROUP_LIMITS, **(group_limits or {})}
        self._groups = {
            group: TokenBucket(limit.rate_per_s, limit.burst, clock=clock)
            for group, limit in limits.items()
        }
        self._global = (
            TokenBucket(global_limit.rate_per_s, global_limit.burst, clock=clock)
            if global_limit is not None
            else None
        )
        self._sleep = sleep

    def _buckets(self, group: EndpointGroup) -> list[TokenBucket]:
        try:
            bucket = self._groups[group]
        except KeyError:
            raise ValueError(f"No rate limit is configured for endpoint group {group}") from None
        return [bucket] if self._global is None else [bucket, self._global]

    async def acquire(self, group: EndpointGroup) -> float:
        """Wait for a token in ``group`` and the global bucket. Returns seconds waited."""
        buckets = self._buckets(group)
        waited = 0.0
        while True:
            wait = max(bucket.wait_time() for bucket in buckets)
            if wait <= _EPSILON:
                for bucket in buckets:
                    bucket.consume()
                return waited
            await self._sleep(wait)
            waited += wait

    def block_group(self, group: EndpointGroup, seconds: float) -> None:
        self._buckets(group)[0].block_for(seconds)

    def block_all(self, seconds: float) -> None:
        for bucket in self._groups.values():
            bucket.block_for(seconds)
        if self._global is not None:
            self._global.block_for(seconds)
