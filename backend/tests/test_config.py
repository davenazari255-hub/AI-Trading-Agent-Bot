import base64
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from mooo_core.config import (
    AIProvider,
    AppEnv,
    BybitEnv,
    Settings,
    WorkerRole,
    format_settings_error,
    load_settings_or_exit,
)
from mooo_core.defaults import RISK_DEFAULTS
from tests.conftest import DB_PASSWORD, VALID_MASTER_KEY


def test_defaults_are_safe(settings: Settings) -> None:
    assert settings.app_env is AppEnv.DEVELOPMENT
    assert settings.bybit_env is BybitEnv.DEMO
    assert settings.allow_live_trading is False
    assert settings.news_providers == []
    assert settings.ai_provider is AIProvider.ANTHROPIC
    assert settings.anthropic_api_key is None
    assert settings.worker_role is WorkerRole.LIVE
    assert settings.worker_lock_ttl_seconds == 15
    assert settings.master_key_bytes() == bytes(range(32))


def test_ceiling_defaults_are_not_below_initial_defaults(settings: Settings) -> None:
    assert settings.ceilings.max_leverage >= RISK_DEFAULTS["maximum_leverage"]
    assert settings.ceilings.min_reward_risk <= RISK_DEFAULTS["minimum_reward_risk"]


@pytest.mark.parametrize(
    "value",
    [
        "short-key",
        base64.b64encode(b"k" * 31).decode(),
        base64.b64encode(b"k" * 33).decode(),
        "zz" * 32,
        "   ",
    ],
)
def test_master_key_must_encode_32_bytes(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MOOO_MASTER_KEY", value)
    with pytest.raises(ValidationError) as info:
        Settings()
    message = format_settings_error(info.value)
    assert "MOOO_MASTER_KEY" in message
    if value.strip():
        assert value not in str(info.value)
        assert value not in message


def test_master_key_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOOO_MASTER_KEY")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    "value",
    [
        "ab" * 32,
        base64.urlsafe_b64encode(bytes(range(200, 232))).decode(),
        base64.urlsafe_b64encode(bytes(range(200, 232))).decode().rstrip("="),
    ],
)
def test_master_key_accepts_hex_and_base64(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MOOO_MASTER_KEY", value)
    assert len(Settings().master_key_bytes()) == 32


def test_live_requires_allow_live_trading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BYBIT_ENV", "live")
    with pytest.raises(ValidationError, match="ALLOW_LIVE_TRADING"):
        Settings()
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "true")
    assert Settings().bybit_env is BybitEnv.LIVE


def test_unknown_bybit_env_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BYBIT_ENV", "mainnet")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("DATABASE_URL", "postgres://mooo:pw@localhost/mooo"),
        ("REDIS_URL", "http://localhost:6379"),
        ("APP_ENV", "staging"),
        ("LOG_LEVEL", "verbose"),
        ("AI_PROVIDER", "openai"),
        ("AI_MODEL", "  "),
        ("WORKER_ROLE", "primary"),
        ("WORKER_LOCK_HEARTBEAT_SECONDS", "10"),
    ],
)
def test_invalid_values_fail_fast(monkeypatch: pytest.MonkeyPatch, key: str, value: str) -> None:
    monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        Settings()


def test_log_level_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert Settings().log_level == "DEBUG"


def test_empty_anthropic_key_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert Settings().anthropic_api_key is None


def test_news_providers_are_parsed_and_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWS_PROVIDERS", " CryptoPanic, bybit_announcements,cryptopanic , ")
    assert Settings().news_providers == ["cryptopanic", "bybit_announcements"]


def test_empty_news_providers_enable_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWS_PROVIDERS", "")
    assert Settings().news_providers == []


def test_invalid_news_provider_name_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWS_PROVIDERS", "bad provider!")
    with pytest.raises(ValidationError):
        Settings()


def test_ceilings_can_be_set_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEILINGS__MAX_LEVERAGE", "8")
    monkeypatch.setenv("CEILINGS__MAX_AI_ANALYSIS_BUDGET_PER_HOUR", "60")
    settings = Settings()
    assert settings.ceilings.max_leverage == Decimal("8")
    assert settings.ceilings.max_ai_analysis_budget_per_hour == 60


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("CEILINGS__MAX_LEVERAGE", "4"),
        ("CEILINGS__MAX_AI_ANALYSIS_BUDGET_PER_HOUR", "39"),
        ("CEILINGS__MIN_REWARD_RISK", "2"),
        ("CEILINGS__MIN_DEEP_ANALYSIS_COOLDOWN_MINUTES", "20"),
        ("CEILINGS__MAX_REGIME_LEVERAGE_CAP", "12"),
        ("CEILINGS__MAX_LEVERAGE_REDUCTION_FACTOR", "1.5"),
        ("CEILINGS__MAX_DAILY_LOSS_PCT", "0"),
        ("CEILINGS__NOT_A_CEILING", "1"),
    ],
)
def test_invalid_ceilings_fail_fast(monkeypatch: pytest.MonkeyPatch, key: str, value: str) -> None:
    monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        Settings()


def test_settings_repr_hides_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value")
    settings = Settings()
    for text in (repr(settings), str(settings), str(settings.public_summary())):
        assert VALID_MASTER_KEY not in text
        assert DB_PASSWORD not in text
        assert "sk-ant-secret-value" not in text
    assert settings.public_summary()["anthropic_api_key_configured"] is True


def test_settings_load_from_dotenv_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MOOO_MASTER_KEY")
    (tmp_path / ".env").write_text(
        f"MOOO_MASTER_KEY={VALID_MASTER_KEY}\nNEWS_PROVIDERS=bybit_announcements\n"
        "POSTGRES_USER=ignored\n",
        encoding="utf-8",
    )
    settings = Settings()
    assert settings.news_providers == ["bybit_announcements"]


def test_load_settings_or_exit_hides_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "not-a-valid-key-but-still-secret"
    monkeypatch.setenv("MOOO_MASTER_KEY", secret)
    with pytest.raises(SystemExit) as info:
        load_settings_or_exit()
    assert info.value.code == 2
    err = capsys.readouterr().err
    assert "MOOO_MASTER_KEY" in err
    assert secret not in err
