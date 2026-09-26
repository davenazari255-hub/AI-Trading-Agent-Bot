import base64
import os
from collections.abc import AsyncIterator

import pytest
from fakeredis import FakeServer
from fakeredis.aioredis import FakeRedis

from mooo_core.config import Settings

MANAGED_ENV_PREFIXES = (
    "APP_ENV",
    "BYBIT_ENV",
    "ALLOW_LIVE_TRADING",
    "WORKER_ROLE",
    "WORKER_HEALTH_PORT",
    "AI_PROVIDER",
    "AI_MODEL",
    "ANTHROPIC_API_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "MOOO_MASTER_KEY",
    "LOG_LEVEL",
    "NEWS_PROVIDERS",
    "CEILINGS",
)


@pytest.fixture
def master_key() -> str:
    return base64.b64encode(bytes(range(32))).decode()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in list(os.environ):
        if name.upper().startswith(MANAGED_ENV_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def settings(clean_env: pytest.MonkeyPatch, master_key: str) -> Settings:
    return Settings(_env_file=None, mooo_master_key=master_key)


@pytest.fixture
async def redis() -> AsyncIterator[FakeRedis]:
    client = FakeRedis(server=FakeServer(), decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()
