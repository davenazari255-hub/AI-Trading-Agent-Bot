import base64
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from mooo_core.config import Settings, get_settings

VALID_MASTER_KEY = base64.b64encode(bytes(range(32))).decode()
DB_PASSWORD = "db-password-must-not-leak"
DATABASE_URL = f"postgresql+asyncpg://mooo:{DB_PASSWORD}@localhost:5432/mooo"
REDIS_URL = "redis://localhost:6379/0"

_SETTINGS_ENV = {
    "APP_ENV",
    "LOG_LEVEL",
    "BYBIT_ENV",
    "ALLOW_LIVE_TRADING",
    "AI_PROVIDER",
    "AI_MODEL",
    "ANTHROPIC_API_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "MOOO_MASTER_KEY",
    "NEWS_PROVIDERS",
    "WORKER_ROLE",
    "WORKER_LOCK_TTL_SECONDS",
    "WORKER_LOCK_HEARTBEAT_SECONDS",
    "WORKER_HEALTH_PORT",
}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Run every test with a clean, valid environment and no .env file."""
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.upper() in _SETTINGS_ENV or key.upper().startswith("CEILINGS__"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("MOOO_MASTER_KEY", VALID_MASTER_KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    return Settings()
