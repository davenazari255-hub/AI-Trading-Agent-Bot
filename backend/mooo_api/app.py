"""FastAPI application factory for the Mooo API Server.

Run with ``uvicorn mooo_api.app:create_app --factory``.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from mooo_api.health import router as health_router
from mooo_core import __version__
from mooo_core.config import Settings, load_settings_or_exit
from mooo_core.log import configure_process_logging


def create_app(settings: Settings | None = None, *, redis: Redis | None = None) -> FastAPI:
    if settings is None:
        # Production entry point (uvicorn --factory): JSON logs with secret redaction.
        resolved = load_settings_or_exit()
        configure_process_logging(resolved)
    else:
        resolved = settings
    owns_redis = redis is None
    client = redis
    if client is None:
        client = Redis.from_url(resolved.redis_url, decode_responses=True)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owns_redis:
                await client.aclose()

    app = FastAPI(title="Mooo API", version=__version__, lifespan=lifespan)
    app.state.settings = resolved
    app.state.redis = client
    app.include_router(health_router)
    return app
