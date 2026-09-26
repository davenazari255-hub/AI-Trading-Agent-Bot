"""FastAPI application factory for the Mooo API Server.

Run with ``uvicorn mooo_api.app:create_app --factory``. When settings come from
the environment (the production path), JSON logging with secret redaction is
configured here. Every router except setup, login, status, and health must be
included with ``include_protected_router``.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import structlog
from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from mooo_api.accounts import OperatorAccountStore, SqlOperatorAccountStore
from mooo_api.auth_routes import PUBLIC_AUTH_PATHS, protected_router, public_router
from mooo_api.auth_service import AuthService, SecurityEventRecorder
from mooo_api.deps import require_session
from mooo_api.errors import install_error_handlers
from mooo_api.health import router as health_router
from mooo_api.login_limiter import LoginLimiter
from mooo_api.sessions import SessionStore
from mooo_core import __version__
from mooo_core.config import Settings, load_settings_or_exit
from mooo_core.db import create_engine
from mooo_core.events import EventRecorder, SqlEventSink
from mooo_core.log import configure_logging
from mooo_core.models import Environment

PUBLIC_PATHS: frozenset[str] = frozenset({"/health", "/api/v1/health", *PUBLIC_AUTH_PATHS})
RECORDER_STOP_TIMEOUT_S = 5.0

logger = structlog.get_logger(__name__)


def include_protected_router(app: FastAPI, router: APIRouter) -> None:
    """Include a router whose routes all require a valid Operator Session."""
    app.include_router(router, dependencies=[Depends(require_session)])


def create_app(
    settings: Settings | None = None,
    *,
    redis: Redis | None = None,
    account_store: OperatorAccountStore | None = None,
    event_recorder: SecurityEventRecorder | None = None,
    password_hasher: PasswordHasher | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    from_environment = settings is None
    resolved = settings if settings is not None else load_settings_or_exit()
    if from_environment:
        configure_logging(resolved.log_level)
    owns_redis = redis is None
    client = (
        redis if redis is not None else Redis.from_url(resolved.redis_url, decode_responses=True)
    )

    engine: AsyncEngine | None = None
    session_factory: async_sessionmaker[AsyncSession] | None = None
    if account_store is None or event_recorder is None:
        engine = create_engine(resolved.database_url)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

    accounts: OperatorAccountStore
    if account_store is not None:
        accounts = account_store
    else:
        assert session_factory is not None
        accounts = SqlOperatorAccountStore(session_factory)

    owned_recorder: EventRecorder | None = None
    recorder: SecurityEventRecorder
    if event_recorder is not None:
        recorder = event_recorder
    else:
        assert session_factory is not None
        owned_recorder = EventRecorder(SqlEventSink(session_factory), publisher=client)
        recorder = owned_recorder

    sessions = SessionStore(client, clock=clock)
    auth_service = AuthService(
        accounts,
        sessions,
        LoginLimiter(client, clock=clock),
        environment=Environment(resolved.bybit_env.value),
        events=recorder,
        hasher=password_hasher,
    )
    stop_recorder = asyncio.Event()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task[None] | None = None
        if owned_recorder is not None:
            task = asyncio.create_task(owned_recorder.run(stop_recorder))
        try:
            yield
        finally:
            stop_recorder.set()
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=RECORDER_STOP_TIMEOUT_S)
                except Exception as exc:
                    logger.warning("event_recorder_stop_failed", error=type(exc).__name__)
            if engine is not None:
                await engine.dispose()
            if owns_redis:
                await client.aclose()

    app = FastAPI(title="Mooo API", version=__version__, lifespan=lifespan)
    app.state.settings = resolved
    app.state.redis = client
    app.state.session_store = sessions
    app.state.auth_service = auth_service
    app.state.event_recorder = recorder
    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(public_router)
    include_protected_router(app, protected_router)
    return app
