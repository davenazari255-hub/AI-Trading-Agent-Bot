import asyncio

import pytest
from fakeredis import FakeAsyncRedis, FakeServer

from mooo_core.config import Settings, WorkerRole
from mooo_worker import main as worker_main
from mooo_worker.lock import WORKER_LOCK_KEY
from mooo_worker.supervisor import TaskSupervisor


def _patch_redis(monkeypatch: pytest.MonkeyPatch, client: FakeAsyncRedis) -> None:
    class _RedisFactory:
        @staticmethod
        def from_url(*_args: object, **_kwargs: object) -> FakeAsyncRedis:
            return client

    monkeypatch.setattr(worker_main, "Redis", _RedisFactory)


async def _wait_for_key(probe: FakeAsyncRedis, timeout: float = 5.0) -> str | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = await probe.get(WORKER_LOCK_KEY)
        if value is not None:
            return str(value)
        await asyncio.sleep(0.01)
    return None


async def test_live_worker_holds_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    server = FakeServer()
    _patch_redis(monkeypatch, FakeAsyncRedis(server=server, decode_responses=True))
    probe = FakeAsyncRedis(server=server, decode_responses=True)

    stop = asyncio.Event()
    live_settings = settings.model_copy(update={"worker_health_port": 0})
    task = asyncio.create_task(worker_main.run_worker(live_settings, stop))
    try:
        owner = await _wait_for_key(probe)
        assert owner is not None
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)
    assert await probe.get(WORKER_LOCK_KEY) is None


async def test_backtest_worker_never_takes_the_lock(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    server = FakeServer()
    _patch_redis(monkeypatch, FakeAsyncRedis(server=server, decode_responses=True))
    probe = FakeAsyncRedis(server=server, decode_responses=True)

    stop = asyncio.Event()
    backtest_settings = settings.model_copy(
        update={"worker_role": WorkerRole.BACKTEST, "worker_health_port": 0}
    )
    task = asyncio.create_task(worker_main.run_worker(backtest_settings, stop))
    try:
        assert await _wait_for_key(probe, timeout=0.3) is None
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)


def test_health_payload_reports_role_and_lock(settings: Settings) -> None:
    supervisor = TaskSupervisor()
    payload = worker_main.build_health_payload(settings, supervisor, None)
    assert payload["service"] == "worker"
    assert payload["role"] == "live"
    assert payload["lock_held"] is None
    assert payload["bybit_env"] == "demo"
