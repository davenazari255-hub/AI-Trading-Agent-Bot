import base64

import pytest
from pydantic import ValidationError

from mooo_core.config import (
    AIProviderName,
    AppEnv,
    BybitEnv,
    Settings,
    WorkerRole,
    decode_master_key,
    format_validation_error,
)


def test_safe_defaults(settings: Settings) -> None:
    assert settings.bybit_env is BybitEnv.DEMO
    assert settings.allow_live_trading is False
    assert settings.news_providers == {}
    assert settings.enabled_news_providers == []
    assert settings.worker_role is WorkerRole.TRADING
    assert settings.app_env is AppEnv.DEVELOPMENT
    assert settings.ai_provider is AIProviderName.ANTHROPIC
    assert settings.anthropic_api_key is None
    assert len(settings.master_key_bytes) == 32


def test_master_key_is_required(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError) as info:
        Settings(_env_file=None)
    assert "MOOO_MASTER_KEY" in format_validation_error(info.value)


def test_empty_master_key_is_treated_as_missing(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("MOOO_MASTER_KEY", "")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("length", [16, 31, 33, 64])
def test_master_key_must_be_32_bytes(clean_env: pytest.MonkeyPatch, length: int) -> None:
    key = base64.b64encode(b"k" * length).decode()
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mooo_master_key=key)


def test_master_key_rejects_non_base64(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mooo_master_key="not base64 !!")


def test_master_key_accepts_standard_and_urlsafe(clean_env: pytest.MonkeyPatch) -> None:
    raw = bytes([251, 255] * 16)
    standard = base64.b64encode(raw).decode()
    urlsafe = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert "+" in standard or "/" in standard
    for value in (standard, urlsafe):
        loaded = Settings(_env_file=None, mooo_master_key=value)
        assert decode_master_key(loaded.mooo_master_key) == raw


def test_environment_variables_are_loaded(clean_env: pytest.MonkeyPatch, master_key: str) -> None:
    clean_env.setenv("MOOO_MASTER_KEY", master_key)
    clean_env.setenv("WORKER_ROLE", "backtest")
    clean_env.setenv("REDIS_URL", "redis://example:6379/1")
    clean_env.setenv("AI_MODEL", "")
    loaded = Settings(_env_file=None)
    assert loaded.worker_role is WorkerRole.BACKTEST
    assert loaded.redis_url == "redis://example:6379/1"
    assert loaded.ai_model is None
    assert loaded.bybit_env is BybitEnv.DEMO


def test_live_requires_allow_flag(clean_env: pytest.MonkeyPatch, master_key: str) -> None:
    with pytest.raises(ValidationError) as info:
        Settings(_env_file=None, mooo_master_key=master_key, bybit_env="live")
    assert "ALLOW_LIVE_TRADING" in format_validation_error(info.value)


def test_live_with_allow_flag_is_accepted(clean_env: pytest.MonkeyPatch, master_key: str) -> None:
    loaded = Settings(
        _env_file=None, mooo_master_key=master_key, bybit_env="live", allow_live_trading=True
    )
    assert loaded.bybit_env is BybitEnv.LIVE


def test_news_providers_are_per_provider_and_opt_in(
    clean_env: pytest.MonkeyPatch, master_key: str
) -> None:
    clean_env.setenv("NEWS_PROVIDERS__CRYPTOPANIC", "true")
    clean_env.setenv("NEWS_PROVIDERS__BYBIT_ANNOUNCEMENTS", "false")
    loaded = Settings(_env_file=None, mooo_master_key=master_key)
    assert loaded.news_providers == {"cryptopanic": True, "bybit_announcements": False}
    assert loaded.enabled_news_providers == ["cryptopanic"]


def test_deployment_ceilings_can_be_overridden(
    clean_env: pytest.MonkeyPatch, master_key: str
) -> None:
    clean_env.setenv("CEILINGS__MAX_LEVERAGE", "8")
    loaded = Settings(_env_file=None, mooo_master_key=master_key)
    assert loaded.ceilings.max_leverage == 8.0
    assert loaded.ceilings.max_watchlist_capacity == 50


def test_invalid_ceiling_fails_fast(clean_env: pytest.MonkeyPatch, master_key: str) -> None:
    clean_env.setenv("CEILINGS__MAX_LEVERAGE", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mooo_master_key=master_key)


def test_regime_cap_ceiling_cannot_exceed_max_leverage(
    clean_env: pytest.MonkeyPatch, master_key: str
) -> None:
    clean_env.setenv("CEILINGS__MAX_LEVERAGE", "4")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mooo_master_key=master_key)


def test_validation_error_does_not_print_secret(clean_env: pytest.MonkeyPatch) -> None:
    secret = base64.b64encode(b"s" * 20).decode()
    with pytest.raises(ValidationError) as info:
        Settings(_env_file=None, mooo_master_key=secret)
    message = format_validation_error(info.value)
    assert secret not in message
    assert "MOOO_MASTER_KEY" in message
