"""SQLAlchemy models for the Mooo PostgreSQL schema.

See the Data Model and Database blueprints. Rules enforced by this module:

* Every trading and analysis table has an ``environment`` column.
* Money and ratio values use NUMERIC(28, 10). Timestamps use TIMESTAMPTZ (UTC).
* Credentials are stored only as ciphertext. There are no plaintext secret columns.
* Opportunity profiles store dimensions only. No combined score or rank column exists.
* No default or target leverage is stored. Leverage limits are upper bounds only.
* ``events`` and ``analysis_snapshots`` are range partitioned by month.
"""

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from mooo_core.db import Base

MONEY = sa.Numeric(28, 10)
TIMESTAMPTZ = sa.DateTime(timezone=True)
ENUM_LENGTH = 32
EVENT_ID_SEQUENCE = "events_id_seq"
PARTITIONED_TABLES: tuple[str, ...] = ("events", "analysis_snapshots")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Environment(enum.StrEnum):
    DEMO = "demo"
    LIVE = "live"
    BACKTEST = "backtest"


class AccountEnvironment(enum.StrEnum):
    DEMO = "demo"
    LIVE = "live"


class AgentState(enum.StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    STOPPING = "stopping"
    HALTED = "halted"


class WatchlistSource(enum.StrEnum):
    ANCHOR = "anchor"
    OPEN_POSITION = "open_position"
    FAST_SCANNER = "fast_scanner"
    AGENT = "agent"


class DecisionOutcome(enum.StrEnum):
    TRADE_PROPOSED = "trade_proposed"
    NO_TRADE = "no_trade"
    POSITION_ACTION = "position_action"


class DeepAnalysisTrigger(enum.StrEnum):
    OPPORTUNITY_SIGNAL = "opportunity_signal"
    REGIME_CHANGE = "regime_change"
    HIGH_IMPACT_NEWS = "high_impact_news"
    AGENT_ATTENTION_REQUEST = "agent_attention_request"
    ANCHOR_REVIEW = "anchor_review"


class DeepAnalysisOutcome(enum.StrEnum):
    COMPLETED = "completed"
    NO_TRADE = "no_trade"
    INVALID = "invalid"
    FAILED = "failed"
    EXPIRED = "expired"


class TradeSide(enum.StrEnum):
    LONG = "long"
    SHORT = "short"


class OrderSide(enum.StrEnum):
    BUY = "buy"
    SELL = "sell"


class RiskOutcome(enum.StrEnum):
    APPROVED = "approved"
    ADJUSTED = "adjusted"
    REJECTED = "rejected"


class OrderStatus(enum.StrEnum):
    PENDING_SUBMIT = "pending_submit"
    SUBMITTED = "submitted"
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class PositionStatus(enum.StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class EventCategory(enum.StrEnum):
    INFO = "INFO"
    MARKET = "MARKET"
    AI = "AI"
    STRATEGY = "STRATEGY"
    RISK = "RISK"
    EXECUTION = "EXECUTION"
    TRADE = "TRADE"
    WARNING = "WARNING"
    ERROR = "ERROR"
    SECURITY = "SECURITY"


class BacktestStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [str(member.value) for member in enum_cls]


def enum_type(enum_cls: type[enum.StrEnum], name: str) -> sa.Enum:
    """VARCHAR plus a named CHECK constraint that lists the enum values."""
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=ENUM_LENGTH,
        values_callable=_enum_values,
        validate_strings=True,
    )


# ---------------------------------------------------------------------------
# Operator, credentials, agent control
# ---------------------------------------------------------------------------


class OperatorAccount(Base):
    __tablename__ = "operator_accounts"
    __table_args__ = (sa.CheckConstraint("singleton", name="singleton"),)

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(sa.Text, unique=True)
    password_hash: Mapped[str] = mapped_column(sa.Text)
    singleton: Mapped[bool] = mapped_column(sa.Boolean, unique=True, default=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class ExchangeCredentialRecord(Base):
    """Envelope-encrypted Bybit credentials. The master key is never stored here."""

    __tablename__ = "exchange_credentials"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    environment: Mapped[AccountEnvironment] = mapped_column(
        enum_type(AccountEnvironment, "account_environment"), unique=True
    )
    api_key_ciphertext: Mapped[bytes] = mapped_column(sa.LargeBinary)
    secret_ciphertext: Mapped[bytes] = mapped_column(sa.LargeBinary)
    wrapped_data_key: Mapped[bytes] = mapped_column(sa.LargeBinary)
    nonces: Mapped[bytes] = mapped_column(sa.LargeBinary)
    key_version: Mapped[int] = mapped_column(sa.Integer)
    masked_key: Mapped[str] = mapped_column(sa.Text)
    key_fingerprint: Mapped[str] = mapped_column(sa.Text)
    permissions: Mapped[dict[str, Any]] = mapped_column(JSONB)
    ip_restricted: Mapped[bool] = mapped_column(sa.Boolean)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class AgentStatusRecord(Base):
    __tablename__ = "agent_status"
    __table_args__ = (sa.CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=False, default=1)
    active_environment: Mapped[AccountEnvironment] = mapped_column(
        enum_type(AccountEnvironment, "account_environment"), default=AccountEnvironment.DEMO
    )
    state: Mapped[AgentState] = mapped_column(
        enum_type(AgentState, "agent_state"), default=AgentState.STOPPED
    )
    halt_reason: Mapped[str | None] = mapped_column(sa.Text)
    halt_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    halted_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class LiveEnablement(Base):
    __tablename__ = "live_enablements"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    key_fingerprint: Mapped[str] = mapped_column(sa.Text)
    equity_at_confirmation: Mapped[Decimal] = mapped_column(MONEY)
    risk_profile_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    confirmed_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)


# ---------------------------------------------------------------------------
# Risk and agent settings
# ---------------------------------------------------------------------------


class RiskProfile(Base):
    """Risk Limits per environment. ``max_leverage`` in ``limits`` is an upper bound only."""

    __tablename__ = "risk_profiles"
    __table_args__ = (
        sa.CheckConstraint("minimum_liquidation_buffer > 0", name="liquidation_buffer_positive"),
        sa.CheckConstraint(
            "leverage_reduction_factor IS NULL OR "
            "(leverage_reduction_factor > 0 AND leverage_reduction_factor <= 1)",
            name="reduction_factor_range",
        ),
        sa.CheckConstraint(
            "regime_leverage_cap IS NULL OR regime_leverage_cap >= 1",
            name="regime_cap_range",
        ),
    )

    environment: Mapped[Environment] = mapped_column(
        enum_type(Environment, "environment"), primary_key=True
    )
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB)
    minimum_liquidation_buffer: Mapped[Decimal] = mapped_column(MONEY)
    regime_leverage_cap: Mapped[Decimal | None] = mapped_column(MONEY)
    leverage_reduction_factor: Mapped[Decimal | None] = mapped_column(MONEY)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class AgentSettings(Base):
    __tablename__ = "agent_settings"
    __table_args__ = (
        sa.CheckConstraint("ai_analysis_budget > 0", name="budget_positive"),
        sa.CheckConstraint("deep_analysis_cooldown_seconds > 0", name="cooldown_positive"),
        sa.CheckConstraint("anchor_review_interval_seconds > 0", name="anchor_review_positive"),
    )

    environment: Mapped[AccountEnvironment] = mapped_column(
        enum_type(AccountEnvironment, "account_environment"), primary_key=True
    )
    ai_analysis_budget: Mapped[int] = mapped_column(sa.Integer)
    deep_analysis_cooldown_seconds: Mapped[int] = mapped_column(sa.Integer)
    anchor_review_interval_seconds: Mapped[int] = mapped_column(sa.Integer)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


# ---------------------------------------------------------------------------
# Market discovery
# ---------------------------------------------------------------------------


class OpportunityProfileSnapshot(Base):
    """Measured dimensions with labels and data timestamps. No combined score."""

    __tablename__ = "opportunity_profile_snapshots"
    __table_args__ = (
        sa.Index(
            "ix_opportunity_profile_snapshots_env_symbol", "environment", "symbol", "captured_at"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    environment: Mapped[Environment] = mapped_column(enum_type(Environment, "environment"))
    symbol: Mapped[str] = mapped_column(sa.Text)
    dimensions: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source_profile_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    captured_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class DynamicWatchlistEntry(Base):
    __tablename__ = "dynamic_watchlist_entries"
    __table_args__ = (
        sa.Index(
            "uq_dynamic_watchlist_entries_active",
            "environment",
            "symbol",
            unique=True,
            postgresql_where=sa.text("removed_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    environment: Mapped[Environment] = mapped_column(enum_type(Environment, "environment"))
    symbol: Mapped[str] = mapped_column(sa.Text)
    source: Mapped[WatchlistSource] = mapped_column(
        enum_type(WatchlistSource, "watchlist_source")
    )
    attention_reason: Mapped[str] = mapped_column(sa.Text)
    admitted_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    last_opportunity_signal_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    removed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)


class DiscoveryFunnel(Base):
    __tablename__ = "discovery_funnels"
    __table_args__ = (
        sa.Index("ix_discovery_funnels_env_completed", "environment", "completed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    environment: Mapped[Environment] = mapped_column(enum_type(Environment, "environment"))
    sweep_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    stage_counts: Mapped[dict[str, Any]] = mapped_column(JSONB)
    completed_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class DiscoverySettings(Base):
    __tablename__ = "discovery_settings"

    environment: Mapped[AccountEnvironment] = mapped_column(
        enum_type(AccountEnvironment, "account_environment"), primary_key=True
    )
    values: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class StrategyConfig(Base):
    __tablename__ = "strategy_configs"

    strategy_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    version: Mapped[str] = mapped_column(sa.Text)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)


# ---------------------------------------------------------------------------
# Analysis, AI runtime, regimes, decisions, risk verdicts
# ---------------------------------------------------------------------------


class AnalysisSnapshot(Base):
    """Partitioned by month on ``created_at``. The primary key includes the partition key."""

    __tablename__ = "analysis_snapshots"
    __table_args__ = (
        sa.Index("ix_analysis_snapshots_env_symbol", "environment", "symbol", "created_at"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, primary_key=True, default=utcnow)
    environment: Mapped[Environment] = mapped_column(enum_type(Environment, "environment"))
    symbol: Mapped[str] = mapped_column(sa.Text)
    features: Mapped[dict[str, Any]] = mapped_column(JSONB)
    regimes: Mapped[dict[str, Any]] = mapped_column(JSONB)
    market_context: Mapped[dict[str, Any]] = mapped_column(JSONB)
    data_as_of: Mapped[datetime] = mapped_column(TIMESTAMPTZ)


class RiskVerdictRecord(Base):
    __tablename__ = "risk_verdicts"
    __table_args__ = (sa.Index("ix_risk_verdicts_env_created", "environment", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    environment: Mapped[Environment] = mapped_column(enum_type(Environment, "environment"))
    proposal: Mapped[dict[str, Any]] = mapped_column(JSONB)
    outcome: Mapped[RiskOutcome] = mapped_column(enum_type(RiskOutcome, "risk_outcome"))
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    adjustments: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    engine_version: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class DecisionRecord(Base):
    """One AI decision. ``analysis_snapshot_id`` has no FK because snapshots are partitioned."""

    __tablename__ = "decision_records"
    __table_args__ = (
        sa.CheckConstraint(
            "outcome <> 'trade_proposed' OR (side IS NOT NULL AND proposed_leverage IS NOT NULL"
            " AND leverage_rationale IS NOT NULL)",
            name="trade_has_leverage_rationale",
        ),
        sa.CheckConstraint(
            "outcome <> 'no_trade' OR no_trade_reason IS NOT NULL", name="no_trade_has_reason"
        ),
        sa.Index("ix_decision_records_env_symbol", "environment", "symbol", "created_at"),
        sa.Index("ix_decision_records_env_correlation", "environment", "correlation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    environment: Mapped[Environment] = mapped_column(enum_type(Environment, "environment"))
    cycle_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    correlation_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    symbol: Mapped[str] = mapped_column(sa.Text)
    outcome: Mapped[DecisionOutcome] = mapped_column(
        enum_type(DecisionOutcome, "decision_outcome")
    )
    deep_analysis_trigger: Mapped[DeepAnalysisTrigger] = mapped_column(
        enum_type(DeepAnalysisTrigger, "deep_analysis_trigger")
    )
    side: Mapped[TradeSide | None] = mapped_column(enum_type(TradeSide, "trade_side"))
    strategy_selection: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    market_regime: Mapped[str | None] = mapped_column(sa.Text)
    opportunity_profile_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("opportunity_profile_snapshots.id")
    )
    selected_candidate: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    not_selected_reason: Mapped[str | None] = mapped_column(sa.Text)
    no_trade_reason: Mapped[str | None] = mapped_column(sa.Text)
    entry_reason: Mapped[str | None] = mapped_column(sa.Text)
    technical_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    market_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    news_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    risk_assessment: Mapped[str | None] = mapped_column(sa.Text)
    entry_price: Mapped[Decimal | None] = mapped_column(MONEY)
    stop_loss: Mapped[Decimal | None] = mapped_column(MONEY)
    take_profit: Mapped[Decimal | None] = mapped_column(MONEY)
    trailing_stop: Mapped[Decimal | None] = mapped_column(MONEY)
    proposed_leverage: Mapped[Decimal | None] = mapped_column(MONEY)
    approved_leverage: Mapped[Decimal | None] = mapped_column(MONEY)
    leverage_rationale: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    position_size: Mapped[Decimal | None] = mapped_column(MONEY)
    risk_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    expected_rr: Mapped[Decimal | None] = mapped_column(MONEY)
    invalidation_condition: Mapped[str | None] = mapped_column(sa.Text)
    # No database FK: analysis_snapshots is partitioned and its key includes created_at.
    # DecisionRepository writes the id of a snapshot it has just stored.
    analysis_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    risk_verdict_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("risk_verdicts.id")
    )
    exit_reason: Mapped[str | None] = mapped_column(sa.Text)
    exit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    realized_pnl: Mapped[Decimal | None] = mapped_column(MONEY)
    fees: Mapped[Decimal | None] = mapped_column(MONEY)
    funding: Mapped[Decimal | None] = mapped_column(MONEY)
    model: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class DeepAnalysisRun(Base):
    """One AI call with trigger, model, duration, and token usage for budget accounting."""

    __tablename__ = "deep_analysis_runs"
    __table_args__ = (
        sa.Index("ix_deep_analysis_runs_env_started", "environment", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    environment: Mapped[Environment] = mapped_column(enum_type(Environment, "environment"))
    correlation_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    trigger_type: Mapped[str] = mapped_column(sa.Text)
    is_thesis_check: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    instruments: Mapped[list[str]] = mapped_column(JSONB)
    model: Mapped[str] = mapped_column(sa.Text)
    input_tokens: Mapped[int] = mapped_column(sa.Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(sa.Integer, default=0)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)
    estimated_cost: Mapped[Decimal | None] = mapped_column(MONEY)
    outcome: Mapped[DeepAnalysisOutcome | None] = mapped_column(
        enum_type(DeepAnalysisOutcome, "deep_analysis_outcome")
    )
    started_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)


class RegimeTransition(Base):
    __tablename__ = "regime_transitions"
    __table_args__ = (
        sa.Index("ix_regime_transitions_env_symbol", "environment", "symbol", "detected_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    environment: Mapped[Environment] = mapped_column(enum_type(Environment, "environment"))
    symbol: Mapped[str] = mapped_column(sa.Text)
    from_regime: Mapped[str | None] = mapped_column(sa.Text)
    to_regime: Mapped[str] = mapped_column(sa.Text)
    detected_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)


# ---------------------------------------------------------------------------
# Orders, fills, positions, equity
# ---------------------------------------------------------------------------


class OrderRecord(Base):
    __tablename__ = "orders"
    __table_args__ = (sa.Index("ix_orders_env_status", "environment", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    environment: Mapped[Environment] = mapped_column(enum_type(Environment, "environment"))
    order_link_id: Mapped[str] = mapped_column(sa.Text, unique=True)
    exchange_order_id: Mapped[str | None] = mapped_column(sa.Text)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("decision_records.id")
    )
    symbol: Mapped[str] = mapped_column(sa.Text)
    side: Mapped[OrderSide] = mapped_column(enum_type(OrderSide, "order_side"))
    order_type: Mapped[str] = mapped_column(sa.Text)
    qty: Mapped[Decimal] = mapped_column(MONEY)
    price: Mapped[Decimal | None] = mapped_column(MONEY)
    trigger_price: Mapped[Decimal | None] = mapped_column(MONEY)
    reduce_only: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    time_in_force: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[OrderStatus] = mapped_column(
        enum_type(OrderStatus, "order_status"), default=OrderStatus.PENDING_SUBMIT
    )
    reject_reason: Mapped[str | None] = mapped_column(sa.Text)
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


class FillRecord(Base):
    __tablename__ = "fills"
    __table_args__ = (sa.Index("ix_fills_env_order_link", "environment", "order_link_id"),)

    exec_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    environment: Mapped[Environment] = mapped_column(enum_type(Environment, "environment"))
    order_link_id: Mapped[str | None] = mapped_column(sa.Text)
    symbol: Mapped[str] = mapped_column(sa.Text)
    side: Mapped[OrderSide] = mapped_column(enum_type(OrderSide, "order_side"))
    price: Mapped[Decimal] = mapped_column(MONEY)
    qty: Mapped[Decimal] = mapped_column(MONEY)
    fee: Mapped[Decimal] = mapped_column(MONEY)
    fee_currency: Mapped[str | None] = mapped_column(sa.Text)
    is_maker: Mapped[bool] = mapped_column(sa.Boolean)
    exec_time: Mapped[datetime] = mapped_column(TIMESTAMPTZ)


class PositionRecord(Base):
    __tablename__ = "positions"
    __table_args__ = (sa.Index("ix_positions_env_status", "environment", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    environment: Mapped[Environment] = mapped_column(enum_type(Environment, "environment"))
    symbol: Mapped[str] = mapped_column(sa.Text)
    side: Mapped[TradeSide] = mapped_column(enum_type(TradeSide, "trade_side"))
    decision_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("decision_records.id")
    )
    is_external: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    status: Mapped[PositionStatus] = mapped_column(
        enum_type(PositionStatus, "position_status"), default=PositionStatus.OPEN
    )
    avg_entry: Mapped[Decimal] = mapped_column(MONEY)
    size: Mapped[Decimal] = mapped_column(MONEY)
    leverage: Mapped[Decimal | None] = mapped_column(MONEY)
    stop_loss: Mapped[Decimal | None] = mapped_column(MONEY)
    take_profit: Mapped[Decimal | None] = mapped_column(MONEY)
    trailing_stop: Mapped[Decimal | None] = mapped_column(MONEY)
    liq_price: Mapped[Decimal | None] = mapped_column(MONEY)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    fees: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    funding: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    opened_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"

    environment: Mapped[Environment] = mapped_column(
        enum_type(Environment, "environment"), primary_key=True
    )
    ts: Mapped[datetime] = mapped_column(TIMESTAMPTZ, primary_key=True)
    equity: Mapped[Decimal] = mapped_column(MONEY)
    wallet_balance: Mapped[Decimal] = mapped_column(MONEY)
    available: Mapped[Decimal] = mapped_column(MONEY)
    used_margin: Mapped[Decimal] = mapped_column(MONEY)
    unrealized_pnl: Mapped[Decimal] = mapped_column(MONEY)
    peak_equity: Mapped[Decimal] = mapped_column(MONEY)
    day_start_equity: Mapped[Decimal] = mapped_column(MONEY)


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


class EventRecord(Base):
    """Partitioned by month on ``ts``. Retention is at least 180 days."""

    __tablename__ = "events"
    __table_args__ = (
        sa.Index("ix_events_env_ts", "environment", "ts"),
        sa.Index("ix_events_env_category_ts", "environment", "category", "ts"),
        {"postgresql_partition_by": "RANGE (ts)"},
    )

    id: Mapped[int] = mapped_column(
        sa.BigInteger,
        sa.Sequence(EVENT_ID_SEQUENCE),
        primary_key=True,
        server_default=sa.text(f"nextval('{EVENT_ID_SEQUENCE}')"),
    )
    ts: Mapped[datetime] = mapped_column(TIMESTAMPTZ, primary_key=True, default=utcnow)
    environment: Mapped[Environment] = mapped_column(enum_type(Environment, "environment"))
    category: Mapped[EventCategory] = mapped_column(enum_type(EventCategory, "event_category"))
    message: Mapped[str] = mapped_column(sa.Text)
    symbol: Mapped[str | None] = mapped_column(sa.Text)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    refs: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


# ---------------------------------------------------------------------------
# News intelligence
# ---------------------------------------------------------------------------


class NewsItem(Base):
    __tablename__ = "news_items"
    __table_args__ = (sa.Index("ix_news_items_published", "published_at"),)

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    dedupe_key: Mapped[str] = mapped_column(sa.Text, unique=True)
    sources: Mapped[list[str]] = mapped_column(JSONB)
    headline: Mapped[str] = mapped_column(sa.Text)
    url: Mapped[str | None] = mapped_column(sa.Text)
    category: Mapped[str | None] = mapped_column(sa.Text)
    assets: Mapped[list[str]] = mapped_column(JSONB)
    published_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ)
    impact_scheduled: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    assessment: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class NewsProviderConfig(Base):
    """Per-provider switch. Only a vault reference to a credential is stored."""

    __tablename__ = "news_provider_configs"
    __table_args__ = (
        sa.CheckConstraint(
            "credential_reference IS NULL OR credential_reference LIKE 'vault:%'",
            name="credential_is_reference",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(sa.Text, unique=True)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    credential_reference: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# Backtesting
# ---------------------------------------------------------------------------


class BacktestRun(Base):
    """Backtest run metadata. Rows produced by a run use environment ``backtest``."""

    __tablename__ = "backtest_runs"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[BacktestStatus] = mapped_column(
        enum_type(BacktestStatus, "backtest_status"), default=BacktestStatus.QUEUED
    )
    progress: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)


metadata = Base.metadata
