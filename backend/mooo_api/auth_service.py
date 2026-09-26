"""AuthService: First-Run Setup, sign-in, sign-out, and password checks.

Passwords are hashed with Argon2id (argon2-cffi). Every sign-in failure returns
one generic message. Five failures within 15 minutes for one client IP or one
username block sign-in for that IP or username for 15 minutes and record a
SECURITY event. Signing out only ends the session; it never sends a command to
the Agent Worker.
"""

import asyncio
import math
import secrets
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

import structlog
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from mooo_api.accounts import AccountExistsError, OperatorAccountStore
from mooo_api.errors import ApiError
from mooo_api.login_limiter import LockoutScope, LoginLimiter
from mooo_api.sessions import OperatorSession, SessionStore
from mooo_core.models import Environment, EventCategory

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
MAX_USERNAME_LENGTH = 64

INVALID_CREDENTIALS_MESSAGE = "Sign-in failed. Check your credentials and try again."
LOCKED_MESSAGE = "Too many failed sign-in attempts. Sign-in is blocked for 15 minutes."
SETUP_DONE_MESSAGE = "First-Run Setup is already complete. Sign in instead."
ACCOUNT_CREATED_EVENT = "Operator Account created by First-Run Setup"
LOCKOUT_EVENT = "Sign-in blocked for 15 minutes after 5 failed attempts"

logger = structlog.get_logger(__name__)


class SecurityEventRecorder(Protocol):
    async def record(
        self,
        environment: Environment,
        category: EventCategory,
        message: str,
        *,
        symbol: str | None = None,
        correlation_id: uuid.UUID | None = None,
        refs: Mapping[str, Any] | None = None,
    ) -> Any: ...


def normalize_username(username: str) -> str:
    return username.strip().lower()


def validate_setup_username(username: str) -> str:
    name = normalize_username(username)
    if not name or len(name) > MAX_USERNAME_LENGTH or not name.isprintable():
        raise ApiError(
            422,
            "invalid_username",
            f"Username must be 1 to {MAX_USERNAME_LENGTH} printable characters.",
        )
    return name


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ApiError(
            422,
            "password_too_short",
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ApiError(
            422,
            "password_too_long",
            f"Password must be at most {MAX_PASSWORD_LENGTH} characters.",
        )


def _setup_done() -> ApiError:
    return ApiError(409, "setup_already_completed", SETUP_DONE_MESSAGE)


class AuthService:
    def __init__(
        self,
        accounts: OperatorAccountStore,
        sessions: SessionStore,
        limiter: LoginLimiter,
        *,
        environment: Environment,
        events: SecurityEventRecorder | None = None,
        hasher: PasswordHasher | None = None,
    ) -> None:
        self._accounts = accounts
        self._sessions = sessions
        self._limiter = limiter
        self._environment = environment
        self._events = events
        self._hasher = hasher if hasher is not None else PasswordHasher()
        # Verified against when the username is unknown, so timing stays the same.
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(24))

    @property
    def session_ttl_s(self) -> int:
        return self._sessions.ttl_s

    async def setup_required(self) -> bool:
        return await self._accounts.get() is None

    async def setup(self, username: str, password: str) -> OperatorSession:
        if not await self.setup_required():
            raise _setup_done()
        name = validate_setup_username(username)
        validate_password(password)
        password_hash = await asyncio.to_thread(self._hasher.hash, password)
        try:
            account = await self._accounts.create(name, password_hash)
        except AccountExistsError:
            raise _setup_done() from None
        await self._record_security(ACCOUNT_CREATED_EVENT, {"username": name})
        logger.info("operator_account_created", username=name)
        return await self._sessions.create(account.id, account.username)

    async def login(self, username: str, password: str, client_ip: str) -> OperatorSession:
        name = normalize_username(username)[:MAX_USERNAME_LENGTH]
        scopes = (LockoutScope("ip", client_ip), LockoutScope("username", name))
        locked_until = await self._limiter.locked_until(scopes)
        if locked_until is not None:
            raise self._locked_error(locked_until)
        account = await self._accounts.get()
        stored_hash: str | None = None
        if account is not None and account.username == name:
            stored_hash = account.password_hash
        valid = await self._verify(stored_hash, password)
        if account is None or not valid:
            newly_locked = await self._limiter.register_failure(scopes)
            for scope in newly_locked:
                await self._record_security(
                    LOCKOUT_EVENT,
                    {"scope": scope.kind, "client_ip": client_ip, "username": name},
                )
                logger.warning("sign_in_locked", scope=scope.kind, client_ip=client_ip)
            if newly_locked:
                raise self._locked_error(await self._limiter.locked_until(scopes))
            raise ApiError(401, "invalid_credentials", INVALID_CREDENTIALS_MESSAGE)
        await self._limiter.clear(scopes)
        logger.info("operator_signed_in", client_ip=client_ip)
        return await self._sessions.create(account.id, account.username)

    async def logout(self, session_id: str) -> None:
        await self._sessions.delete(session_id)
        logger.info("operator_signed_out")

    async def verify_password(self, password: str) -> bool:
        """Check the Operator Account password, for example before enabling Live."""
        account = await self._accounts.get()
        stored_hash = account.password_hash if account is not None else None
        return await self._verify(stored_hash, password)

    async def _verify(self, stored_hash: str | None, password: str) -> bool:
        if len(password) > MAX_PASSWORD_LENGTH:
            return False
        target = stored_hash if stored_hash is not None else self._dummy_hash
        try:
            await asyncio.to_thread(self._hasher.verify, target, password)
        except (VerificationError, InvalidHashError):
            return False
        return stored_hash is not None

    def _locked_error(self, until: float | None) -> ApiError:
        if until is None:
            remaining = self._limiter.lockout_s
        else:
            remaining = until - self._limiter.now()
        retry_after = max(1, math.ceil(remaining))
        return ApiError(
            429,
            "sign_in_locked",
            LOCKED_MESSAGE,
            headers={"Retry-After": str(retry_after)},
        )

    async def _record_security(self, message: str, refs: Mapping[str, Any]) -> None:
        if self._events is None:
            return
        try:
            await self._events.record(
                self._environment, EventCategory.SECURITY, message, refs=refs
            )
        except Exception as exc:
            logger.error("security_event_record_failed", error=type(exc).__name__)
