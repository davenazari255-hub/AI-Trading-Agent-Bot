"""Single active trading instance, enforced by a Redis TTL lock.

Only one Agent Worker may trade an account. The live worker holds the key
``mooo:worker:lock`` with a TTL (15 seconds by default) and renews it on a
heartbeat. If the lock cannot be renewed, including when Redis is down, the
worker marks it as not held and must refuse new trading until it is reacquired.
"""

import asyncio
import inspect
import logging
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

WORKER_LOCK_KEY = "mooo:worker:lock"

LockListener = Callable[[bool], Awaitable[None] | None]
SleepFn = Callable[[float], Awaitable[None]]

# Acquire or renew atomically. Renews only when this owner already holds the key.
_ACQUIRE_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
  return 1
end
if not current then
  redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
  return 1
end
return 0
"""

# Delete only when this owner holds the key.
_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class WorkerLock:
    """Redis TTL lock that allows exactly one live trading worker."""

    def __init__(
        self,
        redis: Redis,
        *,
        key: str = WORKER_LOCK_KEY,
        ttl_seconds: float = 15,
        heartbeat_interval: float = 5,
        owner_id: str | None = None,
        on_change: LockListener | None = None,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if heartbeat_interval <= 0 or heartbeat_interval >= ttl_seconds:
            raise ValueError("heartbeat_interval must be positive and shorter than ttl_seconds")
        self._redis = redis
        self._key = key
        self._ttl_ms = int(ttl_seconds * 1000)
        self._heartbeat_interval = heartbeat_interval
        self._owner_id = owner_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
        self._on_change = on_change
        self._sleep = sleep
        self._held = False

    @property
    def key(self) -> str:
        return self._key

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def held(self) -> bool:
        """True while this worker holds the lock and may trade."""
        return self._held

    async def acquire(self) -> bool:
        """Acquire the lock, or renew it if this owner already holds it."""
        result = await _resolve(
            self._redis.eval(_ACQUIRE_SCRIPT, 1, self._key, self._owner_id, str(self._ttl_ms))
        )
        return int(result) == 1

    async def heartbeat_once(self) -> bool:
        """Acquire or renew once and update :attr:`held`. Never raises on Redis errors."""
        try:
            acquired = await self.acquire()
        except (RedisError, OSError):
            logger.error("Redis unavailable while renewing the worker lock", exc_info=True)
            acquired = False
        await self._set_held(acquired)
        return acquired

    async def maintain(self) -> None:
        """Heartbeat forever. Intended to run under the TaskSupervisor."""
        while True:
            await self.heartbeat_once()
            await self._sleep(self._heartbeat_interval)

    async def release(self) -> bool:
        """Release the lock if this owner holds it."""
        try:
            result = await _resolve(
                self._redis.eval(_RELEASE_SCRIPT, 1, self._key, self._owner_id)
            )
            released = int(result) == 1
        except (RedisError, OSError):
            logger.warning("Could not release the worker lock; it expires with its TTL")
            released = False
        if self._held:
            self._held = False
            logger.info("Released worker lock %s", self._key)
            await self._notify(False)
        return released

    async def _set_held(self, held: bool) -> None:
        if held == self._held:
            return
        self._held = held
        if held:
            logger.info("Acquired worker lock %s", self._key)
        else:
            logger.error(
                "Lost worker lock %s; new trading is refused until it is reacquired", self._key
            )
        await self._notify(held)

    async def _notify(self, held: bool) -> None:
        if self._on_change is None:
            return
        try:
            result = self._on_change(held)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Worker lock listener failed")
