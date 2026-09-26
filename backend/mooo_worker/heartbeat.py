"""Worker heartbeat on Redis so the API Server can report the worker as up or down."""

import asyncio
import json
import time

from redis.asyncio import Redis

from mooo_core.bus import (
    STATE_CHANNEL,
    WORKER_HEARTBEAT_INTERVAL_S,
    WORKER_HEARTBEAT_TTL_S,
    worker_heartbeat_key,
)
from mooo_worker.state import WorkerState


async def publish_heartbeat(redis: Redis, state: WorkerState, *, now: float | None = None) -> None:
    timestamp = time.time() if now is None else now
    payload = json.dumps(
        {
            "type": "worker_heartbeat",
            "role": state.role.value,
            "trading_environment": state.trading_environment.value,
            "lock_held": state.lock_held,
            "ts": timestamp,
        }
    )
    await redis.set(worker_heartbeat_key(state.role), payload, ex=WORKER_HEARTBEAT_TTL_S)
    await redis.publish(STATE_CHANNEL, payload)
    state.last_heartbeat_at = timestamp


async def heartbeat_loop(
    redis: Redis,
    state: WorkerState,
    *,
    interval_s: float = WORKER_HEARTBEAT_INTERVAL_S,
) -> None:
    while True:
        await publish_heartbeat(redis, state)
        await asyncio.sleep(interval_s)
