import pytest
from fakeredis.aioredis import FakeRedis

from mooo_core.bus import WORKER_LOCK_KEY, WORKER_LOCK_TTL_MS
from mooo_core.config import BybitEnv, WorkerRole
from mooo_worker.lock import WorkerLock, maintain_lock
from mooo_worker.state import WorkerState


class _StopLoop(Exception):
    pass


def _trading_state() -> WorkerState:
    return WorkerState(role=WorkerRole.TRADING, trading_environment=BybitEnv.DEMO)


async def test_only_one_instance_holds_the_lock(redis: FakeRedis) -> None:
    first = WorkerLock(redis, token="a")
    second = WorkerLock(redis, token="b")
    assert await first.acquire() is True
    assert await second.acquire() is False
    assert await redis.get(WORKER_LOCK_KEY) == "a"
    ttl = await redis.pttl(WORKER_LOCK_KEY)
    assert 0 < ttl <= WORKER_LOCK_TTL_MS


async def test_only_the_owner_can_extend(redis: FakeRedis) -> None:
    first = WorkerLock(redis, token="a")
    second = WorkerLock(redis, token="b")
    await first.acquire()
    assert await first.extend() is True
    assert await second.extend() is False
    assert second.held is False


async def test_only_the_owner_can_release(redis: FakeRedis) -> None:
    first = WorkerLock(redis, token="a")
    second = WorkerLock(redis, token="b")
    await first.acquire()
    await second.release()
    assert await redis.get(WORKER_LOCK_KEY) == "a"
    await first.release()
    assert await redis.get(WORKER_LOCK_KEY) is None
    assert first.held is False


async def test_lost_lock_is_detected(redis: FakeRedis) -> None:
    lock = WorkerLock(redis, token="a")
    await lock.acquire()
    await redis.set(WORKER_LOCK_KEY, "intruder")
    assert await lock.extend() is False
    assert lock.held is False


async def test_maintain_lock_acquires_then_detects_loss(redis: FakeRedis) -> None:
    state = _trading_state()
    lock = WorkerLock(redis, token="a")
    observed: list[bool] = []

    async def fake_sleep(delay: float) -> None:
        observed.append(state.trading_enabled)
        if len(observed) == 1:
            await redis.set(WORKER_LOCK_KEY, "intruder")
        if len(observed) >= 2:
            raise _StopLoop

    with pytest.raises(_StopLoop):
        await maintain_lock(lock, state, sleep=fake_sleep)
    assert observed == [True, False]
    assert state.lock_held is False


async def test_second_worker_stays_disabled(redis: FakeRedis) -> None:
    await WorkerLock(redis, token="other").acquire()
    state = _trading_state()
    observed: list[bool] = []

    async def fake_sleep(delay: float) -> None:
        observed.append(state.lock_held)
        raise _StopLoop

    with pytest.raises(_StopLoop):
        await maintain_lock(WorkerLock(redis, token="me"), state, sleep=fake_sleep)
    assert observed == [False]
    assert state.trading_enabled is False
