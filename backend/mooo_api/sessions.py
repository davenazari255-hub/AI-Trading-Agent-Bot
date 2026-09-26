"""Server-side Operator Sessions stored in Redis under ``mooo:session:{id}``.

Session ids are 256-bit random values. A session lives for a fixed 12 hours from
sign-in (activity does not extend it) and ends at once on sign-out. If Redis is
unavailable, lookups fail closed and the operator is treated as signed out.
"""

import json
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

SESSION_KEY_PREFIX = "mooo:session:"
SESSION_TTL_S = 12 * 60 * 60
SESSION_ID_BYTES = 32
MAX_SESSION_ID_LENGTH = 128
SESSION_COOKIE = "mooo_session"
CSRF_HEADER = "X-CSRF-Token"


def session_key(session_id: str) -> str:
    return f"{SESSION_KEY_PREFIX}{session_id}"


@dataclass(frozen=True, slots=True)
class OperatorSession:
    session_id: str
    operator_id: uuid.UUID
    username: str
    csrf_token: str
    created_at: float
    expires_at: float


class SessionStore:
    def __init__(
        self,
        redis: Redis,
        *,
        ttl_s: int = SESSION_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if ttl_s < 1:
            raise ValueError("ttl_s must be positive")
        self._redis = redis
        self._ttl_s = ttl_s
        self._clock = clock

    @property
    def ttl_s(self) -> int:
        return self._ttl_s

    async def create(self, operator_id: uuid.UUID, username: str) -> OperatorSession:
        now = self._clock()
        session = OperatorSession(
            session_id=secrets.token_urlsafe(SESSION_ID_BYTES),
            operator_id=operator_id,
            username=username,
            csrf_token=secrets.token_urlsafe(SESSION_ID_BYTES),
            created_at=now,
            expires_at=now + self._ttl_s,
        )
        payload = {
            "operator_id": str(operator_id),
            "username": username,
            "csrf_token": session.csrf_token,
            "created_at": session.created_at,
            "expires_at": session.expires_at,
        }
        await self._redis.set(
            session_key(session.session_id), json.dumps(payload), ex=self._ttl_s
        )
        return session

    async def get(self, session_id: str | None) -> OperatorSession | None:
        if not session_id or len(session_id) > MAX_SESSION_ID_LENGTH:
            return None
        try:
            raw = await self._redis.get(session_key(session_id))
        except (RedisError, OSError):
            return None
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            session = OperatorSession(
                session_id=session_id,
                operator_id=uuid.UUID(str(data["operator_id"])),
                username=str(data["username"]),
                csrf_token=str(data["csrf_token"]),
                created_at=float(data["created_at"]),
                expires_at=float(data["expires_at"]),
            )
        except (ValueError, KeyError, TypeError):
            return None
        if session.expires_at <= self._clock():
            await self.delete(session_id)
            return None
        return session

    async def delete(self, session_id: str) -> None:
        try:
            await self._redis.delete(session_key(session_id))
        except (RedisError, OSError):
            return
