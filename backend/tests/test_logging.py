"""StructuredLogger: JSON output with standard fields and no secrets."""

import io
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
import structlog

from mooo_core.config import Settings
from mooo_core.log import (
    configure_logging,
    correlation_scope,
    current_correlation_id,
    get_logger,
    register_settings_secrets,
)
from mooo_core.redaction import REDACTED, SecretRedactor

API_KEY = "AbCdEfGh12345678"
API_SECRET = "s3cr3tValueXYZ987654"
SESSION_TOKEN = "sess-9f8e7d6c5b4a"
SIGNATURE = "0123456789abcdef"


@contextmanager
def captured_logging(redactor: SecretRedactor) -> Iterator[io.StringIO]:
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    stream = io.StringIO()
    configure_logging("DEBUG", redactor=redactor, stream=stream)
    try:
        yield stream
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
        structlog.reset_defaults()


def _entries(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_entries_are_json_with_standard_fields() -> None:
    with captured_logging(SecretRedactor()) as stream:
        get_logger("mooo.test").info("order placed", symbol="BTCUSDT")

    [entry] = _entries(stream)
    assert entry["event"] == "order placed"
    assert entry["level"] == "info"
    assert entry["category"] == "INFO"
    assert entry["correlation_id"] is None
    assert entry["symbol"] == "BTCUSDT"
    assert entry["logger"] == "mooo.test"
    assert entry["timestamp"]


def test_stdlib_logs_carry_correlation_id_and_category() -> None:
    with captured_logging(SecretRedactor()) as stream, correlation_scope() as correlation_id:
        assert current_correlation_id() == correlation_id
        logging.getLogger("mooo.std").warning("stale data for %s", "ETHUSDT")
        get_logger("mooo.test").info("risk verdict", category="RISK")

    assert current_correlation_id() is None
    warning, verdict = _entries(stream)
    assert warning["event"] == "stale data for ETHUSDT"
    assert warning["level"] == "warning"
    assert warning["category"] == "WARNING"
    assert warning["correlation_id"] == str(correlation_id)
    assert verdict["category"] == "RISK"
    assert verdict["correlation_id"] == str(correlation_id)


def test_secrets_never_reach_log_output() -> None:
    redactor = SecretRedactor([API_SECRET])
    with captured_logging(redactor) as stream:
        log = get_logger("mooo.test")
        std = logging.getLogger("mooo.std")
        log.info(
            "credential submitted",
            api_key=API_KEY,
            api_secret=API_SECRET,
            headers={"X-BAPI-API-KEY": API_KEY, "X-BAPI-SIGN": SIGNATURE},
            cookie=f"mooo_session={SESSION_TOKEN}",
        )
        std.info("signing with %s", API_SECRET)
        std.info("request headers X-BAPI-API-KEY: %s X-BAPI-SIGN: %s", API_KEY, SIGNATURE)
        try:
            raise RuntimeError(f"upstream rejected secret {API_SECRET}")
        except RuntimeError:
            std.exception("call failed")
            log.exception("call failed again")

    output = stream.getvalue()
    for secret in (API_KEY, API_SECRET, SESSION_TOKEN, SIGNATURE):
        assert secret not in output
    assert REDACTED in output
    entries = _entries(stream)
    assert len(entries) == 5
    assert entries[0]["api_key"] == REDACTED
    assert entries[0]["headers"] == {"X-BAPI-API-KEY": REDACTED, "X-BAPI-SIGN": REDACTED}
    for entry in entries[3:]:
        assert entry["category"] == "ERROR"
        assert "RuntimeError" in entry["exception"]


def test_register_settings_secrets(clean_env: pytest.MonkeyPatch, master_key: str) -> None:
    settings = Settings(
        _env_file=None,
        mooo_master_key=master_key,
        anthropic_api_key="sk-ant-test-0123456789",
        database_url="postgresql+asyncpg://mooo:db-pass-123456@postgres:5432/mooo",
        redis_url="redis://:redis-pass-987654@redis:6379/0",
    )
    redactor = SecretRedactor()

    assert register_settings_secrets(settings, redactor) == 4
    out = redactor.redact_text(
        f"{master_key} sk-ant-test-0123456789 db-pass-123456 redis-pass-987654"
    )
    assert out == " ".join([REDACTED] * 4)
