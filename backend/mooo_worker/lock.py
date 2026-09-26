"""Single active trading instance, enforced by a Redis TTL lock (``mooo:worker:lock``).

The trading worker must hold the lock to trade. It refreshes the lock every
5 seconds with a 15-second TTL. If the lock is lost or Redis is unavailable,
new trading is disabled until the lock is held again.
"""

import asyncio
import logging
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from mooo_core.bus import WORKER_LOCK_KEY, WORKER_LOCK_REFRESH_S, WORKER_LOCK_TTL_MS
from mooo_worker.state import WorkerState

logger = logging.getLogger(__name__)

SleepFn = Callable[[float], Awaitable[None]]

_EXTEND_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class WorkerLock:
    def __init__(
        self,
        redis: Redis,
        *,
        token: str | None = None,
        key: str = WORKER_LOCK_KEY,
        ttl_ms: int = WORKER_LOCK_TTL_MS,
    ) -> None:
        self._redis = redis
        self._key = key
        self._ttl_ms = ttl_ms
        self._token = token or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
        self._held = False

    @property
    def token(self) -> str:
        return self._token

    @property
    def held(self) -> bool:
        return self._held

    def mark_lost(self) -> None:
        self._held = False

    async def acquire(self) -> bool:
        acquired = await self._redis.set(self._key, self._token, nx=True, px=self._ttl_ms)
        self._held = bool(acquired)
        return self._held

    async def extend(self) -> bool:
        result = await self._eval(_EXTEND_SCRIPT, self._token, str(self._ttl_ms))
        self._held = int(result) == 1
        return self._held

    async def release(self) -> None:
        try:
            await self._eval(_RELEASE_SCRIPT, self._token)
        finally:
            self._held = False

    async def _eval(self, script: str, *args: str) -> Any:
        client: Any = self._redis
        return await client.eval(script, 1, self._key, *args)


async def maintain_lock(
    lock: WorkerLock,
    state: WorkerState,
    *,
    interval_s: float = WORKER_LOCK_REFRESH_S,
    sleep: SleepFn = asyncio.sleep,
) -> None:
    """Acquire and refresh the worker lock forever and mirror it into ``state``."""
    while True:
        was_held = lock.held
        try:
            if was_held:
                if not await lock.extend():
                    logger.error("Worker lock lost. New trading is disabled.")
            elif await lock.acquire():
                logger.info("Worker lock acquired. This is the active trading instance.")
            else:
                logger.warning("Another worker holds the lock. New trading stays disabled.")
        except (RedisError, OSError):
            lock.mark_lost()
            state.lock_held = False
            logger.error("Redis unavailable. New trading is disabled until the lock is held.")
            raise
        state.lock_held = lock.held
        await sleep(interval_s)
