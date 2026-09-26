from mooo_core.config import DeploymentCeilings
from mooo_core.defaults import (
    AGENT_DEFAULTS,
    ALL_BOUNDED_DEFAULTS,
    ANCHOR_INSTRUMENTS,
    DEFAULTS_LABEL,
    DISCOVERY_DEFAULTS,
    RISK_LIMIT_DEFAULTS,
    BoundKind,
    ceiling_violations,
)


def test_initial_defaults_fit_default_ceilings() -> None:
    assert ceiling_violations(DeploymentCeilings()) == []


def test_every_default_maps_to_a_ceiling_field() -> None:
    fields = set(DeploymentCeilings.model_fields)
    for item in ALL_BOUNDED_DEFAULTS:
        assert item.ceiling_field in fields, item.name


def test_risk_defaults_match_requirements() -> None:
    values = {item.name: item.default for item in RISK_LIMIT_DEFAULTS}
    assert values == {
        "max_loss_per_trade_pct": 0.5,
        "max_account_exposure_pct": 2.0,
        "max_position_size_pct": 25.0,
        "max_leverage": 5.0,
        "min_liquidation_buffer": 2.0,
        "regime_leverage_cap": 3.0,
        "leverage_reduction_factor": 0.5,
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


def test_maximum_leverage_is_an_upper_bound_only() -> None:
    item = next(i for i in RISK_LIMIT_DEFAULTS if i.name == "max_leverage")
    assert item.kind is BoundKind.UPPER


def test_violation_detected_when_ceiling_is_lowered() -> None:
    ceilings = DeploymentCeilings(max_leverage=4, max_regime_leverage_cap=3)
    violations = ceiling_violations(ceilings)
    assert any(v.startswith("max_leverage=") for v in violations)


def test_settings_defaults_are_consistent() -> None:
    assert DISCOVERY_DEFAULTS["watchlist_capacity"] == 20
    assert AGENT_DEFAULTS["ai_analyses_per_hour"] == 40
    assert AGENT_DEFAULTS["deep_analysis_cooldown_minutes"] == 15
    assert AGENT_DEFAULTS["anchor_review_minutes"] == 60
    bounded = {item.name: item.default for item in ALL_BOUNDED_DEFAULTS}
    assert bounded["watchlist_capacity"] == DISCOVERY_DEFAULTS["watchlist_capacity"]
    assert bounded["ai_analyses_per_hour"] == AGENT_DEFAULTS["ai_analyses_per_hour"]
    assert ANCHOR_INSTRUMENTS == ("BTCUSDT", "ETHUSDT")
    assert "not a recommended or optimal value" in DEFAULTS_LABEL.lower()
