import asyncio
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from redis.exceptions import ConnectionError as RedisConnectionError

from mooo_worker.lock import WORKER_LOCK_KEY, WorkerLock


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


def _client(server: FakeServer) -> FakeAsyncRedis:
    return FakeAsyncRedis(server=server, decode_responses=True)


def test_lock_uses_documented_key() -> None:
    assert WORKER_LOCK_KEY == "mooo:worker:lock"


async def test_only_one_owner_can_hold_the_lock(server: FakeServer) -> None:
    first = WorkerLock(_client(server), owner_id="first")
    second = WorkerLock(_client(server), owner_id="second")
    assert await first.acquire() is True
    assert await second.acquire() is False
    assert await first.acquire() is True


async def test_lock_has_15_second_ttl(server: FakeServer) -> None:
    redis = _client(server)
    lock = WorkerLock(redis, owner_id="owner")
    await lock.acquire()
    assert await redis.get(WORKER_LOCK_KEY) == "owner"
    ttl_ms = await redis.pttl(WORKER_LOCK_KEY)
    assert 0 < ttl_ms <= 15_000


async def test_renewal_extends_ttl(server: FakeServer) -> None:
    redis = _client(server)
    lock = WorkerLock(redis, owner_id="owner")
    await lock.acquire()
    await redis.pexpire(WORKER_LOCK_KEY, 1_000)
    assert await lock.heartbeat_once() is True
    assert await redis.pttl(WORKER_LOCK_KEY) > 1_000


async def test_release_only_by_owner(server: FakeServer) -> None:
    redis = _client(server)
    owner = WorkerLock(redis, owner_id="owner")
    other = WorkerLock(_client(server), owner_id="other")
    await owner.heartbeat_once()
    assert await other.release() is False
    assert await redis.get(WORKER_LOCK_KEY) == "owner"
    assert await owner.release() is True
    assert owner.held is False
    assert await redis.get(WORKER_LOCK_KEY) is None
    assert await other.acquire() is True


async def test_heartbeat_detects_takeover_and_reacquires(server: FakeServer) -> None:
    redis = _client(server)
    changes: list[bool] = []
    lock = WorkerLock(redis, owner_id="owner", on_change=changes.append)
    assert await lock.heartbeat_once() is True
    assert lock.held is True

    await redis.set(WORKER_LOCK_KEY, "intruder")
    assert await lock.heartbeat_once() is False
    assert lock.held is False

    await redis.delete(WORKER_LOCK_KEY)
    assert await lock.heartbeat_once() is True
    assert changes == [True, False, True]


async def test_redis_errors_mark_lock_as_not_held() -> None:
    class BrokenRedis:
        async def eval(self, *_args: object) -> object:
            raise RedisConnectionError("redis is down")

    lock = WorkerLock(cast(Any, BrokenRedis()), owner_id="owner")
    assert await lock.heartbeat_once() is False
    assert lock.held is False
    assert await lock.release() is False


async def test_maintain_heartbeats_on_interval(server: FakeServer) -> None:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) >= 3:
            raise asyncio.CancelledError
        await asyncio.sleep(0)

    lock = WorkerLock(_client(server), owner_id="owner", sleep=fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await lock.maintain()
    assert delays == [5, 5, 5]
    assert lock.held is True


@pytest.mark.parametrize(
    ("ttl", "heartbeat"),
    [(0, 1), (15, 0), (15, 15), (15, 20)],
)
def test_invalid_timing_is_rejected(ttl: float, heartbeat: float) -> None:
    with pytest.raises(ValueError):
        WorkerLock(cast(Any, object()), ttl_seconds=ttl, heartbeat_interval=heartbeat)
