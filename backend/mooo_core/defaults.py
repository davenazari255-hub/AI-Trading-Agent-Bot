"""Initial conservative defaults for Operator-editable settings.

These values are safe starting points for a new account. They are not
recommended or optimal values, and the Operator is expected to tune them.
Deployment ceilings in ``mooo_core.config.DeploymentCeilings`` bound what the
dashboard may set. Maximum Leverage is an upper bound only. It is never a
default or target leverage for a trade.
"""

from dataclasses import dataclass
from enum import StrEnum

from mooo_core.config import DeploymentCeilings

DEFAULTS_LABEL = "Initial conservative value. Not a recommended or optimal value."

# Permanent Anchor Instruments. They always stay on the Dynamic Watchlist.
ANCHOR_INSTRUMENTS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")


class BoundKind(StrEnum):
    UPPER = "upper"
    LOWER = "lower"


@dataclass(frozen=True, slots=True)
class BoundedDefault:
    """A default value and the deployment ceiling field that bounds it."""

    name: str
    default: float
    ceiling_field: str
    kind: BoundKind


def _upper(name: str, default: float, ceiling_field: str | None = None) -> BoundedDefault:
    return BoundedDefault(name, default, ceiling_field or name, BoundKind.UPPER)


def _lower(name: str, default: float, ceiling_field: str | None = None) -> BoundedDefault:
    return BoundedDefault(name, default, ceiling_field or name, BoundKind.LOWER)


# AC-RISK-CFG-001.2
RISK_LIMIT_DEFAULTS: tuple[BoundedDefault, ...] = (
    _upper("max_loss_per_trade_pct", 0.5),
    _upper("max_account_exposure_pct", 2.0),
    _upper("max_position_size_pct", 25.0),
    _upper("max_leverage", 5.0),
    _lower("min_liquidation_buffer", 2.0),
    _upper("regime_leverage_cap", 3.0, "max_regime_leverage_cap"),
    _upper("leverage_reduction_factor", 0.5, "max_leverage_reduction_factor"),
    _upper("max_simultaneous_positions", 3),
    _upper("max_daily_loss_pct", 2.0),
    _upper("max_drawdown_pct", 8.0),
    _upper("max_correlated_exposure_pct", 1.0),
    _upper("max_total_margin_usage_pct", 30.0),
    _lower("min_turnover_24h_usdt", 50_000_000),
    _lower("min_reward_to_risk", 1.5),
    _upper("max_spread_pct", 0.05),
    _upper("max_slippage_pct", 0.1),
    _upper("abnormal_volatility_multiple", 3.0, "max_abnormal_volatility_multiple"),
)

# Discovery Settings defaults (Market Discovery feature)
DISCOVERY_DEFAULTS: dict[str, float] = {
    "watchlist_capacity": 20,
    "fast_scan_sweep_seconds": 60,
    "universe_refresh_minutes": 60,
    "min_hourly_turnover_usdt": 2_000_000,
    "min_book_depth_usdt": 100_000,
    "book_depth_band_pct": 0.5,
    "min_atr_1h_pct": 0.25,
    "watchlist_min_stay_minutes": 30,
    "watchlist_idle_expiry_minutes": 90,
    "failed_sweeps_before_removal": 2,
}

# Agent Settings defaults (Autonomous Agent feature)
AGENT_DEFAULTS: dict[str, float] = {
    "ai_analyses_per_hour": 40,
    "deep_analysis_cooldown_minutes": 15,
    "anchor_review_minutes": 60,
    "deep_analysis_queue_expiry_minutes": 15,
    "consecutive_ai_failures_for_kill_switch": 3,
}

SETTINGS_BOUNDED_DEFAULTS: tuple[BoundedDefault, ...] = (
    _upper("watchlist_capacity", 20, "max_watchlist_capacity"),
    _upper("ai_analyses_per_hour", 40, "max_ai_analyses_per_hour"),
    _lower("deep_analysis_cooldown_minutes", 15, "min_deep_analysis_cooldown_minutes"),
    _lower("anchor_review_minutes", 60, "min_anchor_review_minutes"),
)

ALL_BOUNDED_DEFAULTS: tuple[BoundedDefault, ...] = RISK_LIMIT_DEFAULTS + SETTINGS_BOUNDED_DEFAULTS


def ceiling_violations(
    ceilings: DeploymentCeilings,
    items: tuple[BoundedDefault, ...] = ALL_BOUNDED_DEFAULTS,
) -> list[str]:
    """Return a message for each default that is outside its deployment ceiling."""
    violations: list[str] = []
    for item in items:
        limit = float(getattr(ceilings, item.ceiling_field))
        if item.kind is BoundKind.UPPER and item.default > limit:
            violations.append(
                f"{item.name}={item.default} is above {item.ceiling_field}={limit}"
            )
        if item.kind is BoundKind.LOWER and item.default < limit:
            violations.append(
                f"{item.name}={item.default} is below {item.ceiling_field}={limit}"
            )
    return violations
