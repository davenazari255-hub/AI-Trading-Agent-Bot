"""Sign-in lockout counters in Redis.

Each scope (client IP or username) keeps a sorted set of failure times under
``mooo:ratelimit:login:{ip}`` or ``mooo:ratelimit:login:user:{username}``.
Five failures inside 15 minutes set a lock key that blocks sign-in for that
scope for 15 minutes.
"""

import math
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from redis.asyncio import Redis

LOGIN_RATELIMIT_PREFIX = "mooo:ratelimit:login:"
MAX_FAILURES = 5
FAILURE_WINDOW_S = 15 * 60
LOCKOUT_S = 15 * 60
KEY_GRACE_S = 60

ScopeKind = Literal["ip", "username"]


@dataclass(frozen=True, slots=True)
class LockoutScope:
    kind: ScopeKind
    value: str


def failures_key(scope: LockoutScope) -> str:
    if scope.kind == "ip":
        return f"{LOGIN_RATELIMIT_PREFIX}{scope.value}"
    return f"{LOGIN_RATELIMIT_PREFIX}user:{scope.value}"


def lock_key(scope: LockoutScope) -> str:
    return f"{failures_key(scope)}:locked"


class LoginLimiter:
    def __init__(
        self,
        redis: Redis,
        *,
        max_failures: int = MAX_FAILURES,
        window_s: float = FAILURE_WINDOW_S,
        lockout_s: float = LOCKOUT_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_failures < 1 or window_s <= 0 or lockout_s <= 0:
            raise ValueError("max_failures, window_s, and lockout_s must be positive")
        self._redis = redis
        self._max_failures = max_failures
        self._window_s = window_s
        self._lockout_s = lockout_s
        self._clock = clock

    @property
    def lockout_s(self) -> float:
        return self._lockout_s

    def now(self) -> float:
        return self._clock()

    async def locked_until(self, scopes: Sequence[LockoutScope]) -> float | None:
        """Return the latest unlock time among locked scopes, or None."""
        now = self._clock()
        latest: float | None = None
        for scope in scopes:
            raw = await self._redis.get(lock_key(scope))
            if raw is None:
                continue
            try:
                until = float(raw)
            except (TypeError, ValueError):
                continue
            if until > now and (latest is None or until > latest):
                latest = until
        return latest

    async def register_failure(self, scopes: Sequence[LockoutScope]) -> list[LockoutScope]:
        """Record one failure for each scope. Return the scopes that became locked."""
        now = self._clock()
        newly_locked: list[LockoutScope] = []
        for scope in scopes:
            key = failures_key(scope)
            member = f"{now:.6f}-{secrets.token_hex(4)}"
            await self._redis.zadd(key, {member: now})
            await self._redis.zremrangebyscore(key, "-inf", now - self._window_s)
            await self._redis.expire(key, math.ceil(self._window_s) + KEY_GRACE_S)
            if await self._redis.zcard(key) >= self._max_failures:
                await self._redis.set(
                    lock_key(scope),
                    repr(now + self._lockout_s),
                    ex=math.ceil(self._lockout_s) + KEY_GRACE_S,
                )
                await self._redis.delete(key)
                newly_locked.append(scope)
        return newly_locked

    async def clear(self, scopes: Sequence[LockoutScope]) -> None:
        for scope in scopes:
            await self._redis.delete(failures_key(scope))
