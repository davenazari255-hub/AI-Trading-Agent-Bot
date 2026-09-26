"""Pydantic schemas for validated JSONB columns (Data Model blueprint, ADR-001).

These schemas are the only way JSONB documents are written by repositories.
"""

from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, NonNegativeInt, field_validator

from mooo_core.defaults import AGENT_DEFAULTS, DISCOVERY_DEFAULTS, RISK_LIMIT_DEFAULTS

# Risk limits that live in dedicated RiskProfile columns instead of the JSONB document.
RISK_PROFILE_COLUMN_LIMITS: dict[str, str] = {
    "min_liquidation_buffer": "minimum_liquidation_buffer",
    "regime_leverage_cap": "regime_leverage_cap",
    "leverage_reduction_factor": "leverage_reduction_factor",
}

# Keys that would turn an Opportunity Profile into a ranking. They are rejected.
FORBIDDEN_PROFILE_KEYS = frozenset(
    {"score", "combined_score", "composite_score", "overall_score", "total_score", "rank"}
)

PositiveFloat = Annotated[float, Field(gt=0)]
PositiveInt = Annotated[int, Field(gt=0)]
MeasuredValue = float | int | str | bool | None


class RiskLimits(BaseModel):
    """Risk Limits stored in ``risk_profiles.limits``.

    There is no default or target leverage field. ``max_leverage`` is an upper bound only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_loss_per_trade_pct: PositiveFloat
    max_account_exposure_pct: PositiveFloat
    max_position_size_pct: PositiveFloat
    max_leverage: float = Field(ge=1)
    max_simultaneous_positions: PositiveInt
    max_daily_loss_pct: PositiveFloat
    max_drawdown_pct: PositiveFloat
    max_correlated_exposure_pct: PositiveFloat
    max_total_margin_usage_pct: PositiveFloat
    min_turnover_24h_usdt: PositiveFloat
    min_reward_to_risk: PositiveFloat
    max_spread_pct: PositiveFloat
    max_slippage_pct: PositiveFloat
    abnormal_volatility_multiple: PositiveFloat


def initial_risk_limits() -> RiskLimits:
    """Initial conservative values (AC-RISK-CFG-001.2) for the JSONB part of a profile."""
    values = {
        item.name: item.default
        for item in RISK_LIMIT_DEFAULTS
        if item.name not in RISK_PROFILE_COLUMN_LIMITS
    }
    return RiskLimits.model_validate(values)


def initial_risk_columns() -> dict[str, float]:
    """Initial conservative values for the dedicated RiskProfile columns."""
    by_name = {item.name: item.default for item in RISK_LIMIT_DEFAULTS}
    return {column: by_name[name] for name, column in RISK_PROFILE_COLUMN_LIMITS.items()}


def _reject_score_keys(keys: list[str], where: str) -> None:
    bad = sorted(key for key in keys if key.strip().lower() in FORBIDDEN_PROFILE_KEYS)
    if bad:
        raise ValueError(f"Opportunity Profile {where} must not contain a combined score: {bad}")


class OpportunityDimension(BaseModel):
    """One measured dimension with a plain-language label and its data timestamp."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    measurements: dict[str, MeasuredValue] = Field(min_length=1)
    label: str = Field(min_length=1)
    data_as_of: AwareDatetime

    @field_validator("measurements")
    @classmethod
    def _no_score_measurements(cls, value: dict[str, MeasuredValue]) -> dict[str, MeasuredValue]:
        _reject_score_keys(list(value), "measurements")
        return value


class OpportunityProfile(BaseModel):
    """Structured evidence for one Instrument. It never contains a combined score."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimensions: dict[str, OpportunityDimension] = Field(min_length=1)

    @field_validator("dimensions")
    @classmethod
    def _no_score_dimensions(
        cls, value: dict[str, OpportunityDimension]
    ) -> dict[str, OpportunityDimension]:
        _reject_score_keys(list(value), "dimensions")
        return value


class DiscoverySettingsSchema(BaseModel):
    """Operator-editable Discovery Settings stored in ``discovery_settings.values``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    watchlist_capacity: PositiveInt
    fast_scan_sweep_seconds: PositiveInt
    universe_refresh_minutes: PositiveInt
    min_hourly_turnover_usdt: PositiveFloat
    min_book_depth_usdt: PositiveFloat
    book_depth_band_pct: PositiveFloat
    min_atr_1h_pct: PositiveFloat
    watchlist_min_stay_minutes: PositiveInt
    watchlist_idle_expiry_minutes: PositiveInt
    failed_sweeps_before_removal: PositiveInt


def initial_discovery_settings() -> DiscoverySettingsSchema:
    return DiscoverySettingsSchema.model_validate(DISCOVERY_DEFAULTS)


def initial_agent_settings() -> dict[str, int]:
    """Initial Agent Settings columns: budget per hour, cooldown and anchor review in seconds."""
    return {
        "ai_analysis_budget": int(AGENT_DEFAULTS["ai_analyses_per_hour"]),
        "deep_analysis_cooldown_seconds": int(AGENT_DEFAULTS["deep_analysis_cooldown_minutes"] * 60),
        "anchor_review_interval_seconds": int(AGENT_DEFAULTS["anchor_review_minutes"] * 60),
    }


class RiskCheck(BaseModel):
    """One named check inside a Risk Verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    passed: bool
    value: float | str | None = None
    limit: float | str | None = None


FunnelStageCounts = dict[str, NonNegativeInt]
