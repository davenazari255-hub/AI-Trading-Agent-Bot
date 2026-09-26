import io
import json
import logging
from collections.abc import Iterator

import pytest
import structlog
from pydantic import SecretStr

from mooo_core.log import bind_correlation_id, configure_logging, get_logger
from mooo_core.redaction import REDACTED, SecretRedactor, is_sensitive_name

API_KEY = "AbCdEfGh12345678"
API_SECRET = "s3cr3t-VALUE-0123456789abcdef"


@pytest.fixture
def redactor() -> SecretRedactor:
    active = SecretRedactor()
    active.register(API_KEY, SecretStr(API_SECRET))
    return active


@pytest.fixture
def log_stream(redactor: SecretRedactor) -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging("DEBUG", redactor=redactor, stream=stream)
    yield stream
    structlog.contextvars.clear_contextvars()
    structlog.reset_defaults()
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_mooo_handler", False):
            root.removeHandler(handler)


def test_sensitive_names() -> None:
    for name in (
        "api_key",
        "apiKey",
        "API_SECRET",
        "X-BAPI-SIGN",
        "x-bapi-timestamp",
        "Cookie",
        "Set-Cookie",
        "session_token",
        "password",
        "MOOO_MASTER_KEY",
        "Authorization",
    ):
        assert is_sensitive_name(name), name
    for name in ("masked_key", "key_fingerprint", "symbol", "input_tokens", "correlation_id"):
        assert not is_sensitive_name(name), name


def test_registered_values_are_replaced_in_text(redactor: SecretRedactor) -> None:
    output = redactor.redact_text(f"request failed for {API_KEY} using {API_SECRET}")
    assert API_KEY not in output
    assert API_SECRET not in output
    assert output.count(REDACTED) == 2


def test_short_values_are_not_registered() -> None:
    active = SecretRedactor()
    active.register("ab", None, "")
    assert active.redact_text("ab cd") == "ab cd"


def test_unregister_removes_value() -> None:
    active = SecretRedactor()
    active.register("rotating-secret")
    active.unregister("rotating-secret")
    assert active.redact_text("rotating-secret") == "rotating-secret"


def test_inline_headers_query_parameters_and_tokens_are_redacted() -> None:
    active = SecretRedactor()
    samples = {
        "X-BAPI-API-KEY: livekey999": "livekey999",
        "X-BAPI-SIGN=abcdef0123": "abcdef0123",
        "GET /v5/order?api_key=qwerty123&sign=zxcvb987": "qwerty123",
        "Cookie: session=abc123xyz": "abc123xyz",
        "Authorization: Bearer eyJhbGciOi.payload.sig": "eyJhbGciOi",
        'token="tok_live_42"': "tok_live_42",
    }
    for text, secret in samples.items():
        assert secret not in active.redact_text(text), text
    assert active.redact_text("signal: 5 tokens: 1200") == "signal: 5 tokens: 1200"


def test_nested_structures_are_redacted(redactor: SecretRedactor) -> None:
    data = {
        "api_key": "anything",
        "headers": {"X-BAPI-SIGN": "sig", "Content-Type": "application/json"},
        "masked_key": "****5678",
        "items": [{"password": "p"}, f"note {API_SECRET}"],
        "value": SecretStr("hidden-value"),
        "count": 3,
        "missing_token": None,
    }
    output = redactor.redact(data)
    assert output["api_key"] == REDACTED
    assert output["headers"] == {"X-BAPI-SIGN": REDACTED, "Content-Type": "application/json"}
    assert output["masked_key"] == "****5678"
    assert output["items"][0] == {"password": REDACTED}
    assert API_SECRET not in output["items"][1]
    assert output["value"] == REDACTED
    assert output["count"] == 3
    assert output["missing_token"] is None
    assert data["api_key"] == "anything"


def test_structlog_output_never_contains_secrets(log_stream: io.StringIO) -> None:
    log = get_logger("test")
    bind_correlation_id("cid-1")
    log.info(
        f"connecting with {API_KEY}",
        category="SECURITY",
        api_secret=API_SECRET,
        headers={"X-BAPI-API-KEY": API_KEY, "Cookie": "a=b"},
    )
    try:
        raise RuntimeError(f"auth failed secret={API_SECRET}")
    except RuntimeError:
        log.exception("request failed")

    output = log_stream.getvalue()
    assert API_KEY not in output
    assert API_SECRET not in output
    lines = [json.loads(line) for line in output.splitlines() if line.strip()]
    first, second = lines[0], lines[1]
    assert first["category"] == "SECURITY"
    assert first["correlation_id"] == "cid-1"
    assert first["level"] == "info"
    assert "timestamp" in first
    assert second["category"] == "INFO"
    assert "RuntimeError" in second["exception"]


def test_stdlib_logging_is_redacted(log_stream: io.StringIO) -> None:
    logging.getLogger("httpx").warning(
        "HTTP Request: GET https://api-demo.bybit.com/v5/x?api_key=%s", API_KEY
    )
    try:
        raise ValueError(API_SECRET)
    except ValueError:
        logging.getLogger("uvicorn.error").exception("boom")

    output = log_stream.getvalue()
    assert API_KEY not in output
    assert API_SECRET not in output
    lines = [json.loads(line) for line in output.splitlines() if line.strip()]
    assert lines[-1]["event"] == "boom"
    assert "ValueError" in lines[-1]["exception"]
