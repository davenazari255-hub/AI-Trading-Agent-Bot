"""SecretRedactor: keys, secrets, cookies, and X-BAPI headers never reach output."""

import pytest
from pydantic import SecretStr

from mooo_core.redaction import REDACTED, TRUNCATED, SecretRedactor, is_sensitive_name

API_KEY = "AbCdEfGh12345678"
API_SECRET = "s3cr3tValueXYZ987654"
SESSION_TOKEN = "sess-9f8e7d6c5b4a"
SIGNATURE = "0123456789abcdef"
ALL_SECRETS = (API_KEY, API_SECRET, SESSION_TOKEN, SIGNATURE)


@pytest.fixture
def redactor() -> SecretRedactor:
    return SecretRedactor()


@pytest.mark.parametrize(
    "name",
    [
        "api_key",
        "apiKey",
        "API_SECRET",
        "secret",
        "session_token",
        "password",
        "db_password",
        "passphrase",
        "Cookie",
        "set-cookie",
        "Authorization",
        "X-BAPI-API-KEY",
        "X-BAPI-SIGN",
        "x-bapi-timestamp",
        "private_key",
        "sign",
    ],
)
def test_sensitive_field_names(name: str) -> None:
    assert is_sensitive_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "symbol",
        "message",
        "category",
        "correlation_id",
        "input_tokens",
        "masked_key",
        "key_version",
        "Content-Type",
        "monkey",
    ],
)
def test_ordinary_field_names(name: str) -> None:
    assert not is_sensitive_name(name)


def test_nested_structures_are_redacted(redactor: SecretRedactor) -> None:
    data = {
        "symbol": "BTCUSDT",
        "credential": {"api_key": API_KEY, "api_secret": API_SECRET, "masked_key": "****5678"},
        "headers": {
            "X-BAPI-API-KEY": API_KEY,
            "X-BAPI-SIGN": SIGNATURE,
            "Content-Type": "application/json",
        },
        "header_list": [("X-BAPI-SIGN", SIGNATURE), ("Accept", "json")],
        "cookies": {"mooo_session": SESSION_TOKEN},
        "input_tokens": 1234,
        "missing_token": None,
    }

    result = redactor.redact(data)

    assert result["symbol"] == "BTCUSDT"
    assert result["credential"] == {
        "api_key": REDACTED,
        "api_secret": REDACTED,
        "masked_key": "****5678",
    }
    assert result["headers"] == {
        "X-BAPI-API-KEY": REDACTED,
        "X-BAPI-SIGN": REDACTED,
        "Content-Type": "application/json",
    }
    assert result["header_list"] == [("X-BAPI-SIGN", REDACTED), ("Accept", "json")]
    assert result["cookies"] == REDACTED
    assert result["input_tokens"] == 1234
    assert result["missing_token"] is None
    rendered = repr(result)
    for secret in ALL_SECRETS:
        assert secret not in rendered


def test_registered_secrets_are_replaced_in_free_text(redactor: SecretRedactor) -> None:
    assert redactor.register_secret(API_SECRET)
    assert redactor.register_secret(SecretStr(SESSION_TOKEN))

    out = redactor.redact_text(f"request failed for {API_SECRET} in session {SESSION_TOKEN}")

    assert API_SECRET not in out
    assert SESSION_TOKEN not in out
    assert out.count(REDACTED) == 2


def test_registered_secret_inside_unknown_object(redactor: SecretRedactor) -> None:
    class Leaky:
        def __str__(self) -> str:
            return f"Leaky({API_SECRET})"

    redactor.register_secret(API_SECRET)

    assert redactor.redact({"obj": Leaky()}) == {"obj": f"Leaky({REDACTED})"}


def test_unregister_and_short_values(redactor: SecretRedactor) -> None:
    assert not redactor.register_secret("abc")
    assert not redactor.register_secret(None)
    assert redactor.redact_text("abc") == "abc"

    redactor.register_secret("plainvalue123")
    assert redactor.redact_text("x plainvalue123") == f"x {REDACTED}"
    redactor.unregister_secret("plainvalue123")
    assert redactor.redact_text("x plainvalue123") == "x plainvalue123"
    assert redactor.registered_count == 0


@pytest.mark.parametrize(
    "text",
    [
        f"X-BAPI-API-KEY: {API_KEY}",
        f"X-BAPI-SIGN={SIGNATURE}",
        f"GET /v5/order/realtime?api_key={API_KEY}&sign={SIGNATURE}&symbol=BTCUSDT",
        f'{{"api_secret": "{API_SECRET}", "symbol": "BTCUSDT"}}',
        f"Cookie: mooo_session={SESSION_TOKEN}; theme=dark",
        f"Set-Cookie: mooo_session={SESSION_TOKEN}; HttpOnly",
        f"Authorization: Bearer {SESSION_TOKEN}",
        f"password={API_SECRET}",
        f"session_token={SESSION_TOKEN}",
    ],
)
def test_credentials_in_free_text(redactor: SecretRedactor, text: str) -> None:
    out = redactor.redact_text(text)

    assert REDACTED in out
    for secret in ALL_SECRETS:
        assert secret not in out


def test_free_text_keeps_ordinary_values(redactor: SecretRedactor) -> None:
    out = redactor.redact_text(f"GET /v5/order?api_key={API_KEY}&symbol=BTCUSDT")

    assert out == f"GET /v5/order?api_key={REDACTED}&symbol=BTCUSDT"


def test_redaction_is_idempotent(redactor: SecretRedactor) -> None:
    once = redactor.redact_text(f"Cookie: a={SESSION_TOKEN} X-BAPI-SIGN: {SIGNATURE}")

    assert redactor.redact_text(once) == once


def test_secret_types_bytes_and_cycles(redactor: SecretRedactor) -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic

    result = redactor.redact({"note": SecretStr("x"), "blob": b"abc", "cyclic": cyclic})

    assert result["note"] == REDACTED
    assert result["blob"] == "<3 bytes redacted>"
    assert result["cyclic"] == {"self": TRUNCATED}


def test_structlog_processor_interface(redactor: SecretRedactor) -> None:
    redactor.register_secret(API_SECRET)

    out = redactor(None, "info", {"event": f"value {API_SECRET}", "api_key": API_KEY})

    assert out == {"event": f"value {REDACTED}", "api_key": REDACTED}
