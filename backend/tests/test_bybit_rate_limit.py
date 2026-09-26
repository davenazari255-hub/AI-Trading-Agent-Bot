"""Unit tests for the Bybit token-bucket rate limiter (WO-5)."""

import pytest

from mooo_core.exchange import (
    DEFAULT_GROUP_LIMITS,
    GLOBAL_LIMIT,
    EndpointGroup,
    GroupLimit,
    RateLimiter,
    TokenBucket,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_limiter(
    clock: FakeClock,
    limits: dict[EndpointGroup, GroupLimit] | None = None,
    global_limit: GroupLimit | None = None,
) -> RateLimiter:
    return RateLimiter(
        limits, global_limit=global_limit, clock=clock.monotonic, sleep=clock.sleep
    )


def test_every_group_has_a_limit_within_documented_bounds() -> None:
    assert set(DEFAULT_GROUP_LIMITS) == set(EndpointGroup)
    for limit in DEFAULT_GROUP_LIMITS.values():
        assert 0 < limit.rate_per_s <= 50
        assert 1 <= limit.burst <= 50
    for group in (
        EndpointGroup.ORDER_WRITE,
        EndpointGroup.POSITION_WRITE,
        EndpointGroup.ACCOUNT,
        EndpointGroup.USER,
    ):
        assert DEFAULT_GROUP_LIMITS[group].rate_per_s <= 10
    # HTTP IP limit: 600 requests per 5 seconds.
    assert GLOBAL_LIMIT.rate_per_s * 5 < 600
    assert GLOBAL_LIMIT.burst < 600


def test_token_bucket_rejects_invalid_settings() -> None:
    with pytest.raises(ValueError):
        TokenBucket(0, 1)
    with pytest.raises(ValueError):
        TokenBucket(1, 0.5)


async def test_burst_then_steady_rate() -> None:
    clock = FakeClock()
    limiter = make_limiter(clock, {EndpointGroup.ORDER_WRITE: GroupLimit(rate_per_s=2, burst=2)})

    await limiter.acquire(EndpointGroup.ORDER_WRITE)
    await limiter.acquire(EndpointGroup.ORDER_WRITE)
    assert clock.sleeps == []

    await limiter.acquire(EndpointGroup.ORDER_WRITE)
    await limiter.acquire(EndpointGroup.ORDER_WRITE)
    assert clock.sleeps == [pytest.approx(0.5), pytest.approx(0.5)]


async def test_groups_are_independent() -> None:
    clock = FakeClock()
    limiter = make_limiter(
        clock,
        {
            EndpointGroup.ORDER_WRITE: GroupLimit(rate_per_s=1, burst=1),
            EndpointGroup.MARKET: GroupLimit(rate_per_s=1, burst=1),
        },
    )

    await limiter.acquire(EndpointGroup.ORDER_WRITE)
    await limiter.acquire(EndpointGroup.MARKET)
    assert clock.sleeps == []


async def test_global_limit_applies_across_groups() -> None:
    clock = FakeClock()
    limiter = make_limiter(clock, global_limit=GroupLimit(rate_per_s=1, burst=1))

    await limiter.acquire(EndpointGroup.ORDER_WRITE)
    await limiter.acquire(EndpointGroup.MARKET)
    assert clock.sleeps == [pytest.approx(1.0)]


async def test_block_group_waits_until_reset() -> None:
    clock = FakeClock()
    limiter = make_limiter(clock)

    limiter.block_group(EndpointGroup.POSITION_QUERY, 3.0)
    await limiter.acquire(EndpointGroup.MARKET)
    assert clock.sleeps == []

    await limiter.acquire(EndpointGroup.POSITION_QUERY)
    assert sum(clock.sleeps) == pytest.approx(3.0)


async def test_block_all_waits_for_every_group() -> None:
    clock = FakeClock()
    limiter = make_limiter(clock, global_limit=GLOBAL_LIMIT)

    limiter.block_all(2.0)
    await limiter.acquire(EndpointGroup.MARKET)
    assert sum(clock.sleeps) == pytest.approx(2.0)
