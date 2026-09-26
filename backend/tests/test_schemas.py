from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mooo_core.schemas import (
    DiscoverySettingsSchema,
    OpportunityDimension,
    OpportunityProfile,
    RiskLimits,
    initial_agent_settings,
    initial_discovery_settings,
    initial_risk_columns,
    initial_risk_limits,
)

# AC-RISK-CFG-001.2 initial conservative values (JSONB part of the Risk Profile).
CONSERVATIVE_LIMITS = {
    "max_loss_per_trade_pct": 0.5,
    "max_account_exposure_pct": 2.0,
    "max_position_size_pct": 25.0,
    "max_leverage": 5.0,
    "max_simultaneous_positions": 3,
    "max_daily_loss_pct": 2.0,
    "max_drawdown_pct": 8.0,
    "max_correlated_exposure_pct": 1.0,
    "max_total_margin_usage_pct": 30.0,
    "min_turnover_24h_usdt": 50_000_000,
    "min_reward_to_risk": 1.5,
    "max_spread_pct": 0.05,
    "max_slippage_pct": 0.1,
    "abnormal_volatility_multiple": 3.0,
}
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _dimension(**measurements: float) -> OpportunityDimension:
    return OpportunityDimension(measurements=measurements, label="Measured", data_as_of=NOW)


def test_initial_risk_limits_match_conservative_defaults() -> None:
    assert initial_risk_limits().model_dump() == CONSERVATIVE_LIMITS


def test_initial_risk_columns_match_conservative_defaults() -> None:
    assert initial_risk_columns() == {
        "minimum_liquidation_buffer": 2.0,
        "regime_leverage_cap": 3.0,
        "leverage_reduction_factor": 0.5,
    }


def test_risk_limits_have_no_default_leverage_field() -> None:
    leverage_fields = [name for name in RiskLimits.model_fields if "leverage" in name]
    assert leverage_fields == ["max_leverage"]


def test_risk_limits_reject_a_default_leverage_value() -> None:
    data = {**CONSERVATIVE_LIMITS, "default_leverage": 3}
    with pytest.raises(ValidationError):
        RiskLimits.model_validate(data)


def test_risk_limits_reject_max_leverage_below_one() -> None:
    with pytest.raises(ValidationError):
        RiskLimits.model_validate({**CONSERVATIVE_LIMITS, "max_leverage": 0.5})


def test_opportunity_profile_keeps_labels_and_timestamps() -> None:
    profile = OpportunityProfile(dimensions={"liquidity": _dimension(turnover_1h_usdt=2.5e6)})
    dumped = profile.model_dump(mode="json")
    assert dumped["dimensions"]["liquidity"]["label"] == "Measured"
    assert dumped["dimensions"]["liquidity"]["data_as_of"].startswith("2026-09-26T12:00:00")


@pytest.mark.parametrize("key", ["score", "Combined_Score", "rank", "overall_score"])
def test_opportunity_profile_rejects_combined_score_dimension(key: str) -> None:
    with pytest.raises(ValidationError):
        OpportunityProfile(dimensions={key: _dimension(value=1.0)})


def test_opportunity_dimension_rejects_score_measurement() -> None:
    with pytest.raises(ValidationError):
        _dimension(score=0.93)


def test_opportunity_profile_rejects_top_level_score() -> None:
    with pytest.raises(ValidationError):
        OpportunityProfile.model_validate(
            {
                "dimensions": {
                    "trend": {
                        "measurements": {"adx": 31},
                        "label": "Strong trend",
                        "data_as_of": NOW.isoformat(),
                    }
                },
                "score": 0.9,
            }
        )


def test_opportunity_dimension_requires_timezone_aware_timestamp() -> None:
    with pytest.raises(ValidationError):
        OpportunityDimension(
            measurements={"atr_pct": 1.2}, label="Volatile", data_as_of=datetime(2026, 9, 26)
        )


def test_initial_agent_settings() -> None:
    assert initial_agent_settings() == {
        "ai_analysis_budget": 40,
        "deep_analysis_cooldown_seconds": 900,
        "anchor_review_interval_seconds": 3600,
    }


def test_initial_discovery_settings_and_unknown_keys() -> None:
    settings = initial_discovery_settings()
    assert settings.watchlist_capacity == 20
    assert settings.fast_scan_sweep_seconds == 60
    with pytest.raises(ValidationError):
        DiscoverySettingsSchema.model_validate({**settings.model_dump(), "unknown": 1})
