"""Typed runtime settings shared by the API Server and the Agent Worker.

A single ``.env`` file (or the process environment) drives :class:`Settings`.
Invalid values stop the process at startup. Error output names the setting but
never prints its value, so secrets cannot leak through validation errors.
"""

import base64
import binascii
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from mooo_core.defaults import AGENT_DEFAULTS, DISCOVERY_DEFAULTS, RISK_DEFAULTS

MASTER_KEY_BYTES = 32

_PROVIDER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class BybitEnv(StrEnum):
    """Initial Active Environment. Live still requires Live Trading Enablement."""

    DEMO = "demo"
    LIVE = "live"


class WorkerRole(StrEnum):
    LIVE = "live"
    BACKTEST = "backtest"


class AIProvider(StrEnum):
    ANTHROPIC = "anthropic"


def decode_master_key(raw: str) -> bytes:
    """Decode ``MOOO_MASTER_KEY`` from hex or base64 and check it is 32 bytes.

    The error message never includes the key material.
    """
    value = raw.strip()
    if not value:
        raise ValueError("MOOO_MASTER_KEY is required")
    if len(value) == MASTER_KEY_BYTES * 2:
        try:
            return bytes.fromhex(value)
        except ValueError:
            pass
    standard = value.replace("-", "+").replace("_", "/")
    standard += "=" * (-len(standard) % 4)
    try:
        decoded = base64.b64decode(standard, validate=True)
    except (binascii.Error, ValueError):
        decoded = b""
    if len(decoded) != MASTER_KEY_BYTES:
        raise ValueError(
            f"MOOO_MASTER_KEY must encode exactly {MASTER_KEY_BYTES} bytes as base64 or hex "
            "(generate one with: openssl rand -base64 32)"
        )
    return decoded


@dataclass(frozen=True)
class _CeilingSpec:
    field: str
    defaults: Mapping[str, Decimal | int]
    setting: str
    upper: bool


_CEILING_SPECS: tuple[_CeilingSpec, ...] = (
    # Discovery breadth and cadence
    _CeilingSpec(
        "max_dynamic_watchlist_capacity", DISCOVERY_DEFAULTS, "dynamic_watchlist_capacity", True
    ),
    _CeilingSpec(
        "min_fast_scan_interval_seconds", DISCOVERY_DEFAULTS, "fast_scan_interval_seconds", False
    ),
    _CeilingSpec(
        "min_instrument_refresh_minutes", DISCOVERY_DEFAULTS, "instrument_refresh_minutes", False
    ),
    # Agent Settings
    _CeilingSpec(
        "max_ai_analysis_budget_per_hour", AGENT_DEFAULTS, "ai_analysis_budget_per_hour", True
    ),
    _CeilingSpec(
        "min_deep_analysis_cooldown_minutes",
        AGENT_DEFAULTS,
        "deep_analysis_cooldown_minutes",
        False,
    ),
    _CeilingSpec(
        "min_anchor_review_interval_minutes",
        AGENT_DEFAULTS,
        "anchor_review_interval_minutes",
        False,
    ),
    # Risk Limits
    _CeilingSpec("max_loss_per_trade_pct", RISK_DEFAULTS, "maximum_loss_per_trade_pct", True),
    _CeilingSpec("max_account_exposure_pct", RISK_DEFAULTS, "maximum_account_exposure_pct", True),
    _CeilingSpec("max_position_size_pct", RISK_DEFAULTS, "maximum_position_size_pct", True),
    _CeilingSpec("max_leverage", RISK_DEFAULTS, "maximum_leverage", True),
    _CeilingSpec("max_regime_leverage_cap", RISK_DEFAULTS, "regime_leverage_cap", True),
    _CeilingSpec(
        "max_leverage_reduction_factor", RISK_DEFAULTS, "leverage_reduction_factor", True
    ),
    _CeilingSpec("min_liquidation_buffer", RISK_DEFAULTS, "minimum_liquidation_buffer", False),
    _CeilingSpec(
        "max_simultaneous_positions", RISK_DEFAULTS, "maximum_simultaneous_positions", True
    ),
    _CeilingSpec("max_daily_loss_pct", RISK_DEFAULTS, "maximum_daily_loss_pct", True),
    _CeilingSpec("max_drawdown_pct", RISK_DEFAULTS, "maximum_drawdown_pct", True),
    _CeilingSpec(
        "max_correlated_exposure_pct", RISK_DEFAULTS, "maximum_correlated_exposure_pct", True
    ),
    _CeilingSpec(
        "max_total_margin_usage_pct", RISK_DEFAULTS, "maximum_total_margin_usage_pct", True
    ),
    _CeilingSpec("min_turnover_24h_usdt", RISK_DEFAULTS, "minimum_turnover_24h_usdt", False),
    _CeilingSpec("min_reward_risk", RISK_DEFAULTS, "minimum_reward_risk", False),
    _CeilingSpec("max_spread_pct", RISK_DEFAULTS, "maximum_spread_pct", True),
    _CeilingSpec("max_slippage_pct", RISK_DEFAULTS, "maximum_slippage_pct", True),
)


class DeploymentCeilings(BaseModel):
    """Hard bounds that dashboard profiles cannot exceed.

    ``max_*`` fields are upper bounds and ``min_*`` fields are lower bounds.
    Ceilings are deployment limits. They are kept separate from the initial
    conservative defaults in :mod:`mooo_core.defaults` and are not targets.
    Set them with ``CEILINGS__<FIELD>`` environment variables.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Discovery
    max_dynamic_watchlist_capacity: int = Field(default=50, gt=0)
    min_fast_scan_interval_seconds: int = Field(default=15, gt=0)
    min_instrument_refresh_minutes: int = Field(default=15, gt=0)
    # Agent
    max_ai_analysis_budget_per_hour: int = Field(default=120, gt=0)
    min_deep_analysis_cooldown_minutes: int = Field(default=5, gt=0)
    min_anchor_review_interval_minutes: int = Field(default=15, gt=0)
    # Risk
    max_loss_per_trade_pct: Decimal = Field(default=Decimal("2"), gt=0, le=100)
    max_account_exposure_pct: Decimal = Field(default=Decimal("6"), gt=0, le=100)
    max_position_size_pct: Decimal = Field(default=Decimal("50"), gt=0, le=100)
    max_leverage: Decimal = Field(default=Decimal("10"), ge=1)
    max_regime_leverage_cap: Decimal = Field(default=Decimal("5"), ge=1)
    max_leverage_reduction_factor: Decimal = Field(default=Decimal("1"), gt=0, le=1)
    min_liquidation_buffer: Decimal = Field(default=Decimal("1.5"), gt=0)
    max_simultaneous_positions: int = Field(default=10, gt=0)
    max_daily_loss_pct: Decimal = Field(default=Decimal("5"), gt=0, le=100)
    max_drawdown_pct: Decimal = Field(default=Decimal("20"), gt=0, le=100)
    max_correlated_exposure_pct: Decimal = Field(default=Decimal("3"), gt=0, le=100)
    max_total_margin_usage_pct: Decimal = Field(default=Decimal("60"), gt=0, le=100)
    min_turnover_24h_usdt: Decimal = Field(default=Decimal("10000000"), gt=0)
    min_reward_risk: Decimal = Field(default=Decimal("1"), gt=0)
    max_spread_pct: Decimal = Field(default=Decimal("0.2"), gt=0, le=100)
    max_slippage_pct: Decimal = Field(default=Decimal("0.5"), gt=0, le=100)

    @model_validator(mode="after")
    def _check_against_defaults(self) -> Self:
        problems: list[str] = []
        for spec in _CEILING_SPECS:
            value = Decimal(str(getattr(self, spec.field)))
            default = Decimal(str(spec.defaults[spec.setting]))
            name = f"CEILINGS__{spec.field.upper()}"
            if spec.upper and value < default:
                problems.append(f"{name} must be at least the initial default {default}")
            if not spec.upper and value > default:
                problems.append(f"{name} must be at most the initial default {default}")
        if self.max_regime_leverage_cap > self.max_leverage:
            problems.append(
                "CEILINGS__MAX_REGIME_LEVERAGE_CAP must not exceed CEILINGS__MAX_LEVERAGE"
            )
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def as_public_dict(self) -> dict[str, Any]:
        """Return ceilings for validation and settings responses. Contains no secrets."""
        return self.model_dump(mode="json")


class Settings(BaseSettings):
    """Deployment settings loaded from the environment and ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    app_env: AppEnv = AppEnv.DEVELOPMENT
    log_level: LogLevel = "INFO"

    bybit_env: BybitEnv = BybitEnv.DEMO
    allow_live_trading: bool = False

    ai_provider: AIProvider = AIProvider.ANTHROPIC
    ai_model: str = "claude-opus-5-5"
    anthropic_api_key: SecretStr | None = None

    database_url: str = Field(repr=False)
    redis_url: str = Field(repr=False)
    mooo_master_key: SecretStr

    news_providers: Annotated[list[str], NoDecode] = Field(default_factory=list)

    worker_role: WorkerRole = WorkerRole.LIVE
    worker_lock_ttl_seconds: int = Field(default=15, ge=5, le=300)
    worker_lock_heartbeat_seconds: float = Field(default=5.0, gt=0)
    worker_health_port: int = Field(default=8081, ge=1, le=65535)

    ceilings: DeploymentCeilings = Field(default_factory=DeploymentCeilings)

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("anthropic_api_key", mode="before")
    @classmethod
    def _empty_secret_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("ai_model")
    @classmethod
    def _check_ai_model(cls, value: str) -> str:
        model = value.strip()
        if not model:
            raise ValueError("AI_MODEL must not be empty")
        return model

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: str) -> str:
        url = value.strip()
        if not url.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use the postgresql+asyncpg:// scheme")
        return url

    @field_validator("redis_url")
    @classmethod
    def _check_redis_url(cls, value: str) -> str:
        url = value.strip()
        if not url.startswith(("redis://", "rediss://")):
            raise ValueError("REDIS_URL must use the redis:// or rediss:// scheme")
        return url

    @field_validator("mooo_master_key")
    @classmethod
    def _check_master_key(cls, value: SecretStr) -> SecretStr:
        decode_master_key(value.get_secret_value())
        return value

    @field_validator("news_providers", mode="before")
    @classmethod
    def _parse_news_providers(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            items = value.split(",")
        elif isinstance(value, list | tuple):
            items = [str(item) for item in value]
        else:
            raise ValueError("NEWS_PROVIDERS must be a comma-separated list of provider names")
        providers: list[str] = []
        for item in items:
            name = item.strip().lower()
            if not name:
                continue
            if not _PROVIDER_NAME.fullmatch(name):
                raise ValueError(f"Invalid news provider name in NEWS_PROVIDERS: {name!r}")
            if name not in providers:
                providers.append(name)
        return providers

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.bybit_env is BybitEnv.LIVE and not self.allow_live_trading:
            raise ValueError(
                "BYBIT_ENV=live requires ALLOW_LIVE_TRADING=true. "
                "Live still requires Live Trading Enablement in the dashboard."
            )
        if self.worker_lock_heartbeat_seconds * 2 > self.worker_lock_ttl_seconds:
            raise ValueError(
                "WORKER_LOCK_HEARTBEAT_SECONDS must be at most half of WORKER_LOCK_TTL_SECONDS"
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env is AppEnv.PRODUCTION

    def master_key_bytes(self) -> bytes:
        """Return the 32-byte Credential Vault master key."""
        return decode_master_key(self.mooo_master_key.get_secret_value())

    def public_summary(self) -> dict[str, Any]:
        """Return non-secret settings that are safe to log or show in responses."""
        return {
            "app_env": self.app_env.value,
            "bybit_env": self.bybit_env.value,
            "allow_live_trading": self.allow_live_trading,
            "ai_provider": self.ai_provider.value,
            "ai_model": self.ai_model,
            "anthropic_api_key_configured": self.anthropic_api_key is not None,
            "news_providers": list(self.news_providers),
            "worker_role": self.worker_role.value,
            "ceilings": self.ceilings.as_public_dict(),
        }


def format_settings_error(exc: ValidationError) -> str:
    """Format a settings validation error without any input values."""
    lines = ["Invalid Mooo configuration:"]
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        location = "__".join(str(part) for part in error["loc"]).upper() or "SETTINGS"
        lines.append(f"  {location}: {error['msg']}")
    return "\n".join(lines)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache settings for the current process."""
    return Settings()


def load_settings_or_exit() -> Settings:
    """Load settings or stop the process with exit code 2 and a secret-free message."""
    try:
        return get_settings()
    except ValidationError as exc:
        print(format_settings_error(exc), file=sys.stderr)
        raise SystemExit(2) from None
