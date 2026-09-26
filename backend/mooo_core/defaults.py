"""Initial conservative defaults for operator-editable settings.

These are the safe starting values listed in the requirements (Market Discovery,
Autonomous Agent, and Risk Configuration). They are not recommended optimal values.
Deployment Ceilings in :mod:`mooo_core.config` bound how far an operator can move them.
"""

from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType

DEFAULTS_LABEL = "Initial conservative defaults. Not optimal values."

DISCOVERY_DEFAULTS: Mapping[str, Decimal | int] = MappingProxyType(
    {
        "fast_scan_interval_seconds": 60,
        "instrument_refresh_minutes": 60,
        "minimum_hourly_turnover_usdt": Decimal("2000000"),
        "minimum_book_depth_usdt": Decimal("100000"),
        "minimum_volatility_pct": Decimal("0.25"),
        "dynamic_watchlist_capacity": 20,
        "watchlist_minimum_stay_minutes": 30,
        "watchlist_idle_expiry_minutes": 90,
        "volume_spike_multiple": Decimal("3"),
        "open_interest_surge_pct": Decimal("5"),
        "funding_extreme_pct": Decimal("0.05"),
    }
)

AGENT_DEFAULTS: Mapping[str, Decimal | int] = MappingProxyType(
    {
        "ai_analysis_budget_per_hour": 40,
        "deep_analysis_cooldown_minutes": 15,
        "anchor_review_interval_minutes": 60,
    }
)

RISK_DEFAULTS: Mapping[str, Decimal | int] = MappingProxyType(
    {
        "maximum_loss_per_trade_pct": Decimal("0.5"),
        "maximum_account_exposure_pct": Decimal("2"),
        "maximum_position_size_pct": Decimal("25"),
        "maximum_leverage": Decimal("5"),
        "minimum_liquidation_buffer": Decimal("2"),
        "regime_leverage_cap": Decimal("3"),
        "leverage_reduction_factor": Decimal("0.5"),
        "maximum_simultaneous_positions": 3,
        "maximum_daily_loss_pct": Decimal("2"),
        "maximum_drawdown_pct": Decimal("8"),
        "maximum_correlated_exposure_pct": Decimal("1"),
        "maximum_total_margin_usage_pct": Decimal("30"),
        "minimum_turnover_24h_usdt": Decimal("50000000"),
        "minimum_reward_risk": Decimal("1.5"),
        "maximum_spread_pct": Decimal("0.05"),
        "maximum_slippage_pct": Decimal("0.1"),
        "abnormal_volatility_multiple": Decimal("3"),
    }
)
