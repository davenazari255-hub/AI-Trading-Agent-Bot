"""Health endpoints for the API Server (``/health`` and ``/api/v1/health``)."""

import json
import time
from typing import Any

from fastapi import APIRouter, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

from mooo_core import __version__
from mooo_core.bus import WORKER_DOWN_AFTER_S, worker_heartbeat_key
from mooo_core.config import Settings, WorkerRole

router = APIRouter(tags=["health"])


async def read_worker_status(redis: Redis, *, now: float | None = None) -> dict[str, Any]:
    """Report the trading worker as up when its heartbeat is at most 20 seconds old."""
    try:
        raw = await redis.get(worker_heartbeat_key(WorkerRole.TRADING))
    except (RedisError, OSError):
        return {"status": "unknown"}
    if raw is None:
        return {"status": "down"}
    try:
        data = json.loads(raw)
        timestamp = float(data["ts"])
    except (ValueError, KeyError, TypeError):
        return {"status": "unknown"}
    current = time.time() if now is None else now
    age = max(current - timestamp, 0.0)
    return {
        "status": "up" if age <= WORKER_DOWN_AFTER_S else "down",
        "last_heartbeat_age_s": round(age, 1),
        "lock_held": bool(data.get("lock_held", False)),
    }


async def _redis_status(redis: Redis) -> str:
    try:
        await redis.ping()
    except (RedisError, OSError):
        return "unavailable"
    return "ok"


@router.get("/health")
@router.get("/api/v1/health")
async def health(request: Request) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    redis: Redis = request.app.state.redis
    return {
        "status": "ok",
        "service": "api",
        "version": __version__,
        "trading_environment": settings.bybit_env.value,
        "live_trading_allowed": settings.allow_live_trading,
        "redis": await _redis_status(redis),
        "worker": await read_worker_status(redis),
    }
