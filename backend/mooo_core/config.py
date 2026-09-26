"""Typed settings for the Mooo Python containers.

Settings come from the environment or a local ``.env`` file. Startup fails fast
with a readable message when a value is invalid. Secret values are never printed.

Safety defaults: ``BYBIT_ENV`` is ``demo`` and ``ALLOW_LIVE_TRADING`` is ``false``.
No news provider is enabled by default.
"""

import base64
import binascii
import sys
from enum import StrEnum
from functools import lru_cache
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

MASTER_KEY_BYTES = 32


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class BybitEnv(StrEnum):
    """Initial Active Environment. Demo Trading is the default."""

    DEMO = "demo"
    LIVE = "live"


class WorkerRole(StrEnum):
    TRADING = "trading"
    BACKTEST = "backtest"


class AIProviderName(StrEnum):
    ANTHROPIC = "anthropic"


class DeploymentCeilings(BaseModel):
    """Hard bounds for Operator-editable settings.

    Ceilings are limits. They are not defaults and not targets. Dashboard profiles
    are validated against them. ``max_*`` fields are upper bounds and ``min_*``
    fields are lower bounds (floors). Override with ``CEILINGS__<FIELD>``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Discovery Settings
    max_watchlist_capacity: int = Field(default=50, ge=1, le=500)

    # Agent Settings
    max_ai_analyses_per_hour: int = Field(default=120, ge=1, le=10_000)
    min_deep_analysis_cooldown_minutes: int = Field(default=5, ge=0, le=1_440)
    min_anchor_review_minutes: int = Field(default=15, ge=1, le=1_440)

    # Risk Limits: upper bounds
    max_loss_per_trade_pct: float = Field(default=2.0, gt=0, le=100)
    max_account_exposure_pct: float = Field(default=10.0, gt=0, le=100)
    max_position_size_pct: float = Field(default=100.0, gt=0, le=1_000)
    max_leverage: float = Field(default=10.0, ge=1, le=100)
    max_regime_leverage_cap: float = Field(default=5.0, ge=1, le=100)
    max_leverage_reduction_factor: float = Field(default=1.0, gt=0, le=1)
    max_simultaneous_positions: int = Field(default=10, ge=1, le=100)
    max_daily_loss_pct: float = Field(default=10.0, gt=0, le=100)
    max_drawdown_pct: float = Field(default=25.0, gt=0, le=100)
    max_correlated_exposure_pct: float = Field(default=5.0, gt=0, le=100)
    max_total_margin_usage_pct: float = Field(default=80.0, gt=0, le=100)
    max_spread_pct: float = Field(default=0.5, gt=0, le=10)
    max_slippage_pct: float = Field(default=1.0, gt=0, le=10)
    max_abnormal_volatility_multiple: float = Field(default=10.0, gt=1, le=100)

    # Risk Limits: lower bounds (floors)
    min_liquidation_buffer: float = Field(default=1.5, ge=1, le=20)
    min_turnover_24h_usdt: float = Field(default=5_000_000, ge=0)
    min_reward_to_risk: float = Field(default=1.0, gt=0, le=20)

    @model_validator(mode="after")
    def _regime_cap_within_max_leverage(self) -> Self:
        if self.max_regime_leverage_cap > self.max_leverage:
            raise ValueError(
                "CEILINGS__MAX_REGIME_LEVERAGE_CAP must not exceed CEILINGS__MAX_LEVERAGE"
            )
        return self


def decode_master_key(value: SecretStr) -> bytes:
    """Decode MOOO_MASTER_KEY (standard or URL-safe base64) and check it is 32 bytes."""
    raw = value.get_secret_value().strip()
    normalized = raw.replace("-", "+").replace("_", "/")
    normalized += "=" * (-len(normalized) % 4)
    try:
        key = base64.b64decode(normalized, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("MOOO_MASTER_KEY must be base64-encoded") from exc
    if len(key) != MASTER_KEY_BYTES:
        raise ValueError(f"MOOO_MASTER_KEY must decode to exactly {MASTER_KEY_BYTES} bytes")
    return key


class Settings(BaseSettings):
    """Deployment settings shared by the API Server and the Agent Worker."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        env_ignore_empty=True,
        case_sensitive=False,
        extra="ignore",
    )

    app_env: AppEnv = AppEnv.DEVELOPMENT
    bybit_env: BybitEnv = BybitEnv.DEMO
    allow_live_trading: bool = False
    worker_role: WorkerRole = WorkerRole.TRADING

    ai_provider: AIProviderName = AIProviderName.ANTHROPIC
    ai_model: str | None = None
    anthropic_api_key: SecretStr | None = None

    database_url: str = "postgresql+asyncpg://mooo:mooo@postgres:5432/mooo"
    redis_url: str = "redis://redis:6379/0"

    mooo_master_key: SecretStr

    news_providers: dict[str, bool] = Field(default_factory=dict)
    ceilings: DeploymentCeilings = Field(default_factory=DeploymentCeilings)

    log_level: str = "INFO"
    worker_health_port: int = Field(default=8081, ge=1, le=65_535)

    @field_validator("mooo_master_key")
    @classmethod
    def _validate_master_key(cls, value: SecretStr) -> SecretStr:
        decode_master_key(value)
        return value

    @field_validator("news_providers")
    @classmethod
    def _normalize_news_providers(cls, value: dict[str, bool]) -> dict[str, bool]:
        return {name.strip().lower(): enabled for name, enabled in value.items() if name.strip()}

    @model_validator(mode="after")
    def _live_requires_deployment_permission(self) -> Self:
        if self.bybit_env is BybitEnv.LIVE and not self.allow_live_trading:
            raise ValueError(
                "BYBIT_ENV=live requires ALLOW_LIVE_TRADING=true. Live Trading also needs "
                "Live Trading Enablement in the dashboard."
            )
        return self

    @property
    def master_key_bytes(self) -> bytes:
        return decode_master_key(self.mooo_master_key)

    @property
    def enabled_news_providers(self) -> list[str]:
        return sorted(name for name, enabled in self.news_providers.items() if enabled)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def format_validation_error(exc: ValidationError) -> str:
    """Return a readable summary of configuration errors without input values."""
    lines = ["Invalid Mooo configuration:"]
    for error in exc.errors(include_input=False, include_url=False):
        location = "__".join(str(part) for part in error["loc"]).upper() or "SETTINGS"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


def load_settings_or_exit() -> Settings:
    """Load settings, or print a readable error and exit with status 2."""
    try:
        return get_settings()
    except ValidationError as exc:
        print(format_validation_error(exc), file=sys.stderr)
        raise SystemExit(2) from None
