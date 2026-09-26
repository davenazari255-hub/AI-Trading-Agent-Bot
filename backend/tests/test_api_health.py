import json
import time

import httpx
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI

from mooo_api.app import create_app
from mooo_core.bus import worker_heartbeat_key
from mooo_core.config import BybitEnv, Settings, WorkerRole
from mooo_worker.heartbeat import publish_heartbeat
from mooo_worker.state import WorkerState


async def _get(app: FastAPI, path: str = "/api/v1/health") -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def test_health_reports_demo_defaults_and_worker_down(
    settings: Settings, redis: FakeRedis
) -> None:
    response = await _get(create_app(settings, redis=redis))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["trading_environment"] == "demo"
    assert body["live_trading_allowed"] is False
    assert body["redis"] == "ok"
    assert body["worker"]["status"] == "down"


async def test_worker_is_up_after_heartbeat(settings: Settings, redis: FakeRedis) -> None:
    state = WorkerState(
        role=WorkerRole.TRADING, trading_environment=BybitEnv.DEMO, lock_held=True
    )
    await publish_heartbeat(redis, state)
    response = await _get(create_app(settings, redis=redis), "/health")
    worker = response.json()["worker"]
    assert worker["status"] == "up"
    assert worker["lock_held"] is True
    assert state.last_heartbeat_at is not None


async def test_stale_heartbeat_reports_down(settings: Settings, redis: FakeRedis) -> None:
    stale = json.dumps({"ts": time.time() - 30, "lock_held": True})
    await redis.set(worker_heartbeat_key(WorkerRole.TRADING), stale)
    response = await _get(create_app(settings, redis=redis))
    assert response.json()["worker"]["status"] == "down"


async def test_backtest_heartbeat_does_not_count_as_trading_worker(
    settings: Settings, redis: FakeRedis
) -> None:
    state = WorkerState(role=WorkerRole.BACKTEST, trading_environment=BybitEnv.DEMO)
    await publish_heartbeat(redis, state)
    response = await _get(create_app(settings, redis=redis))
    assert response.json()["worker"]["status"] == "down"
