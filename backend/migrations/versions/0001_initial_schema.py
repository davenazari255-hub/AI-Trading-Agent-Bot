"""Initial schema: discovery, AI runtime, regimes, news, risk, trading, events.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-26

The tables are declared here as a frozen copy so later model changes need their own
revisions. ``events`` and ``analysis_snapshots`` are range partitioned by month.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from mooo_core.db import NAMING_CONVENTION

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(28, 10)
TS = sa.DateTime(timezone=True)
PARTITIONED = ("events", "analysis_snapshots")

ENVIRONMENT = ("demo", "live", "backtest")
ACCOUNT_ENVIRONMENT = ("demo", "live")

metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=32)


def _env() -> sa.Column[str]:
    return sa.Column("environment", _enum("environment", *ENVIRONMENT), nullable=False)


def _id() -> sa.Column[object]:
    return sa.Column("id", sa.Uuid, primary_key=True)


sa.Table(
    "operator_accounts",
    metadata,
    _id(),
    sa.Column("username", sa.Text, nullable=False, unique=True),
    sa.Column("password_hash", sa.Text, nullable=False),
    sa.Column("singleton", sa.Boolean, nullable=False, unique=True),
    sa.Column("created_at", TS, nullable=False),
    sa.CheckConstraint("singleton", name="singleton"),
)

sa.Table(
    "exchange_credentials",
    metadata,
    _id(),
    sa.Column(
        "environment",
        _enum("account_environment", *ACCOUNT_ENVIRONMENT),
        nullable=False,
        unique=True,
    ),
    sa.Column("api_key_ciphertext", sa.LargeBinary, nullable=False),
    sa.Column("secret_ciphertext", sa.LargeBinary, nullable=False),
    sa.Column("wrapped_data_key", sa.LargeBinary, nullable=False),
    sa.Column("nonces", sa.LargeBinary, nullable=False),
    sa.Column("key_version", sa.Integer, nullable=False),
    sa.Column("masked_key", sa.Text, nullable=False),
    sa.Column("key_fingerprint", sa.Text, nullable=False),
    sa.Column("permissions", JSONB, nullable=False),
    sa.Column("ip_restricted", sa.Boolean, nullable=False),
    sa.Column("created_at", TS, nullable=False),
)

sa.Table(
    "agent_status",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column(
        "active_environment",
        _enum("account_environment", *ACCOUNT_ENVIRONMENT),
        nullable=False,
        server_default="demo",
    ),
    sa.Column(
        "state",
        _enum("agent_state", "stopped", "running", "stopping", "halted"),
        nullable=False,
        server_default="stopped",
    ),
    sa.Column("halt_reason", sa.Text, nullable=True),
    sa.Column("halt_details", JSONB, nullable=True),
    sa.Column("halted_at", TS, nullable=True),
    sa.Column("updated_at", TS, nullable=False),
    sa.CheckConstraint("id = 1", name="singleton"),
)

sa.Table(
    "live_enablements",
    metadata,
    _id(),
    sa.Column("key_fingerprint", sa.Text, nullable=False),
    sa.Column("equity_at_confirmation", MONEY, nullable=False),
    sa.Column("risk_profile_snapshot", JSONB, nullable=False),
    sa.Column("confirmed_at", TS, nullable=False),
    sa.Column("revoked_at", TS, nullable=True),
)

sa.Table(
    "risk_profiles",
    metadata,
    sa.Column("environment", _enum("environment", *ENVIRONMENT), primary_key=True),
    sa.Column("limits", JSONB, nullable=False),
    sa.Column("minimum_liquidation_buffer", MONEY, nullable=False),
    sa.Column("regime_leverage_cap", MONEY, nullable=True),
    sa.Column("leverage_reduction_factor", MONEY, nullable=True),
    sa.Column("updated_at", TS, nullable=False),
    sa.CheckConstraint("minimum_liquidation_buffer > 0", name="liquidation_buffer_positive"),
    sa.CheckConstraint(
        "leverage_reduction_factor IS NULL OR "
        "(leverage_reduction_factor > 0 AND leverage_reduction_factor <= 1)",
        name="reduction_factor_range",
    ),
    sa.CheckConstraint(
        "regime_leverage_cap IS NULL OR regime_leverage_cap >= 1", name="regime_cap_range"
    ),
)

sa.Table(
    "agent_settings",
    metadata,
    sa.Column(
        "environment", _enum("account_environment", *ACCOUNT_ENVIRONMENT), primary_key=True
    ),
    sa.Column("ai_analysis_budget", sa.Integer, nullable=False),
    sa.Column("deep_analysis_cooldown_seconds", sa.Integer, nullable=False),
    sa.Column("anchor_review_interval_seconds", sa.Integer, nullable=False),
    sa.Column("updated_at", TS, nullable=False),
    sa.CheckConstraint("ai_analysis_budget > 0", name="budget_positive"),
    sa.CheckConstraint("deep_analysis_cooldown_seconds > 0", name="cooldown_positive"),
    sa.CheckConstraint("anchor_review_interval_seconds > 0", name="anchor_review_positive"),
)

sa.Table(
    "opportunity_profile_snapshots",
    metadata,
    _id(),
    _env(),
    sa.Column("symbol", sa.Text, nullable=False),
    sa.Column("dimensions", JSONB, nullable=False),
    sa.Column("source_profile_id", sa.Uuid, nullable=True),
    sa.Column("captured_at", TS, nullable=False),
    sa.Index(
        "ix_opportunity_profile_snapshots_env_symbol", "environment", "symbol", "captured_at"
    ),
)

sa.Table(
    "dynamic_watchlist_entries",
    metadata,
    _id(),
    _env(),
    sa.Column("symbol", sa.Text, nullable=False),
    sa.Column(
        "source",
        _enum("watchlist_source", "anchor", "open_position", "fast_scanner", "agent"),
        nullable=False,
    ),
    sa.Column("attention_reason", sa.Text, nullable=False),
    sa.Column("admitted_at", TS, nullable=False),
    sa.Column("last_opportunity_signal_at", TS, nullable=True),
    sa.Column("removed_at", TS, nullable=True),
    sa.Index(
        "uq_dynamic_watchlist_entries_active",
        "environment",
        "symbol",
        unique=True,
        postgresql_where=sa.text("removed_at IS NULL"),
    ),
)

sa.Table(
    "discovery_funnels",
    metadata,
    _id(),
    _env(),
    sa.Column("sweep_id", sa.Uuid, nullable=False),
    sa.Column("stage_counts", JSONB, nullable=False),
    sa.Column("completed_at", TS, nullable=False),
    sa.Index("ix_discovery_funnels_env_completed", "environment", "completed_at"),
)

sa.Table(
    "discovery_settings",
    metadata,
    sa.Column(
        "environment", _enum("account_environment", *ACCOUNT_ENVIRONMENT), primary_key=True
    ),
    sa.Column("values", JSONB, nullable=False),
    sa.Column("updated_at", TS, nullable=False),
)

sa.Table(
    "strategy_configs",
    metadata,
    sa.Column("strategy_id", sa.Text, primary_key=True),
    sa.Column("version", sa.Text, nullable=False),
    sa.Column("enabled", sa.Boolean, nullable=False),
    sa.Column("params", JSONB, nullable=False),
)

sa.Table(
    "analysis_snapshots",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("created_at", TS, primary_key=True),
    _env(),
    sa.Column("symbol", sa.Text, nullable=False),
    sa.Column("features", JSONB, nullable=False),
    sa.Column("regimes", JSONB, nullable=False),
    sa.Column("market_context", JSONB, nullable=False),
    sa.Column("data_as_of", TS, nullable=False),
    sa.Index("ix_analysis_snapshots_env_symbol", "environment", "symbol", "created_at"),
    postgresql_partition_by="RANGE (created_at)",
)

sa.Table(
    "risk_verdicts",
    metadata,
    _id(),
    _env(),
    sa.Column("proposal", JSONB, nullable=False),
    sa.Column(
        "outcome", _enum("risk_outcome", "approved", "adjusted", "rejected"), nullable=False
    ),
    sa.Column("checks", JSONB, nullable=False),
    sa.Column("adjustments", JSONB, nullable=True),
    sa.Column("engine_version", sa.Text, nullable=False),
    sa.Column("created_at", TS, nullable=False),
    sa.Index("ix_risk_verdicts_env_created", "environment", "created_at"),
)

sa.Table(
    "decision_records",
    metadata,
    _id(),
    _env(),
    sa.Column("cycle_id", sa.Uuid, nullable=False),
    sa.Column("correlation_id", sa.Uuid, nullable=False),
    sa.Column("symbol", sa.Text, nullable=False),
    sa.Column(
        "outcome",
        _enum("decision_outcome", "trade_proposed", "no_trade", "position_action"),
        nullable=False,
    ),
    sa.Column(
        "deep_analysis_trigger",
        _enum(
            "deep_analysis_trigger",
            "opportunity_signal",
            "regime_change",
            "high_impact_news",
            "agent_attention_request",
            "anchor_review",
        ),
        nullable=False,
    ),
    sa.Column("side", _enum("trade_side", "long", "short"), nullable=True),
    sa.Column("strategy_selection", JSONB, nullable=True),
    sa.Column("market_regime", sa.Text, nullable=True),
    sa.Column(
        "opportunity_profile_snapshot_id",
        sa.Uuid,
        sa.ForeignKey("opportunity_profile_snapshots.id"),
        nullable=True,
    ),
    sa.Column("selected_candidate", sa.Boolean, nullable=False),
    sa.Column("not_selected_reason", sa.Text, nullable=True),
    sa.Column("no_trade_reason", sa.Text, nullable=True),
    sa.Column("entry_reason", sa.Text, nullable=True),
    sa.Column("technical_evidence", JSONB, nullable=True),
    sa.Column("market_context", JSONB, nullable=True),
    sa.Column("news_context", JSONB, nullable=True),
    sa.Column("risk_assessment", sa.Text, nullable=True),
    sa.Column("entry_price", MONEY, nullable=True),
    sa.Column("stop_loss", MONEY, nullable=True),
    sa.Column("take_profit", MONEY, nullable=True),
    sa.Column("trailing_stop", MONEY, nullable=True),
    sa.Column("proposed_leverage", MONEY, nullable=True),
    sa.Column("approved_leverage", MONEY, nullable=True),
    sa.Column("leverage_rationale", JSONB, nullable=True),
    sa.Column("position_size", MONEY, nullable=True),
    sa.Column("risk_amount", MONEY, nullable=True),
    sa.Column("expected_rr", MONEY, nullable=True),
    sa.Column("invalidation_condition", sa.Text, nullable=True),
    sa.Column("analysis_snapshot_id", sa.Uuid, nullable=True),
    sa.Column("risk_verdict_id", sa.Uuid, sa.ForeignKey("risk_verdicts.id"), nullable=True),
    sa.Column("exit_reason", sa.Text, nullable=True),
    sa.Column("exit_price", MONEY, nullable=True),
    sa.Column("realized_pnl", MONEY, nullable=True),
    sa.Column("fees", MONEY, nullable=True),
    sa.Column("funding", MONEY, nullable=True),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("created_at", TS, nullable=False),
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

sa.Table(
    "deep_analysis_runs",
    metadata,
    _id(),
    _env(),
    sa.Column("correlation_id", sa.Uuid, nullable=False),
    sa.Column("trigger_type", sa.Text, nullable=False),
    sa.Column("is_thesis_check", sa.Boolean, nullable=False),
    sa.Column("instruments", JSONB, nullable=False),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("input_tokens", sa.Integer, nullable=False),
    sa.Column("output_tokens", sa.Integer, nullable=False),
    sa.Column("duration_ms", sa.Integer, nullable=True),
    sa.Column("estimated_cost", MONEY, nullable=True),
    sa.Column(
        "outcome",
        _enum("deep_analysis_outcome", "completed", "no_trade", "invalid", "failed", "expired"),
        nullable=True,
    ),
    sa.Column("started_at", TS, nullable=False),
    sa.Column("completed_at", TS, nullable=True),
    sa.Index("ix_deep_analysis_runs_env_started", "environment", "started_at"),
)

sa.Table(
    "regime_transitions",
    metadata,
    _id(),
    _env(),
    sa.Column("symbol", sa.Text, nullable=False),
    sa.Column("from_regime", sa.Text, nullable=True),
    sa.Column("to_regime", sa.Text, nullable=False),
    sa.Column("detected_at", TS, nullable=False),
    sa.Column("source_event_id", sa.Uuid, nullable=True),
    sa.Index("ix_regime_transitions_env_symbol", "environment", "symbol", "detected_at"),
)

ORDER_STATUS = (
    "pending_submit",
    "submitted",
    "new",
    "partially_filled",
    "filled",
    "cancelled",
    "rejected",
    "unknown",
)

sa.Table(
    "orders",
    metadata,
    _id(),
    _env(),
    sa.Column("order_link_id", sa.Text, nullable=False, unique=True),
    sa.Column("exchange_order_id", sa.Text, nullable=True),
    sa.Column("decision_id", sa.Uuid, sa.ForeignKey("decision_records.id"), nullable=True),
    sa.Column("symbol", sa.Text, nullable=False),
    sa.Column("side", _enum("order_side", "buy", "sell"), nullable=False),
    sa.Column("order_type", sa.Text, nullable=False),
    sa.Column("qty", MONEY, nullable=False),
    sa.Column("price", MONEY, nullable=True),
    sa.Column("trigger_price", MONEY, nullable=True),
    sa.Column("reduce_only", sa.Boolean, nullable=False),
    sa.Column("time_in_force", sa.Text, nullable=True),
    sa.Column("status", _enum("order_status", *ORDER_STATUS), nullable=False),
    sa.Column("reject_reason", sa.Text, nullable=True),
    sa.Column("attempts", sa.Integer, nullable=False),
    sa.Column("created_at", TS, nullable=False),
    sa.Column("updated_at", TS, nullable=False),
    sa.Index("ix_orders_env_status", "environment", "status"),
)

sa.Table(
    "fills",
    metadata,
    sa.Column("exec_id", sa.Text, primary_key=True),
    _env(),
    sa.Column("order_link_id", sa.Text, nullable=True),
    sa.Column("symbol", sa.Text, nullable=False),
    sa.Column("side", _enum("order_side", "buy", "sell"), nullable=False),
    sa.Column("price", MONEY, nullable=False),
    sa.Column("qty", MONEY, nullable=False),
    sa.Column("fee", MONEY, nullable=False),
    sa.Column("fee_currency", sa.Text, nullable=True),
    sa.Column("is_maker", sa.Boolean, nullable=False),
    sa.Column("exec_time", TS, nullable=False),
    sa.Index("ix_fills_env_order_link", "environment", "order_link_id"),
)

sa.Table(
    "positions",
    metadata,
    _id(),
    _env(),
    sa.Column("symbol", sa.Text, nullable=False),
    sa.Column("side", _enum("trade_side", "long", "short"), nullable=False),
    sa.Column("decision_id", sa.Uuid, sa.ForeignKey("decision_records.id"), nullable=True),
    sa.Column("is_external", sa.Boolean, nullable=False),
    sa.Column("status", _enum("position_status", "open", "closed"), nullable=False),
    sa.Column("avg_entry", MONEY, nullable=False),
    sa.Column("size", MONEY, nullable=False),
    sa.Column("leverage", MONEY, nullable=True),
    sa.Column("stop_loss", MONEY, nullable=True),
    sa.Column("take_profit", MONEY, nullable=True),
    sa.Column("trailing_stop", MONEY, nullable=True),
    sa.Column("liq_price", MONEY, nullable=True),
    sa.Column("realized_pnl", MONEY, nullable=False),
    sa.Column("fees", MONEY, nullable=False),
    sa.Column("funding", MONEY, nullable=False),
    sa.Column("opened_at", TS, nullable=False),
    sa.Column("closed_at", TS, nullable=True),
    sa.Index("ix_positions_env_status", "environment", "status"),
)

sa.Table(
    "equity_snapshots",
    metadata,
    sa.Column("environment", _enum("environment", *ENVIRONMENT), primary_key=True),
    sa.Column("ts", TS, primary_key=True),
    sa.Column("equity", MONEY, nullable=False),
    sa.Column("wallet_balance", MONEY, nullable=False),
    sa.Column("available", MONEY, nullable=False),
    sa.Column("used_margin", MONEY, nullable=False),
    sa.Column("unrealized_pnl", MONEY, nullable=False),
    sa.Column("peak_equity", MONEY, nullable=False),
    sa.Column("day_start_equity", MONEY, nullable=False),
)

EVENT_CATEGORIES = (
    "INFO",
    "MARKET",
    "AI",
    "STRATEGY",
    "RISK",
    "EXECUTION",
    "TRADE",
    "WARNING",
    "ERROR",
    "SECURITY",
)

sa.Table(
    "events",
    metadata,
    sa.Column(
        "id",
        sa.BigInteger,
        sa.Sequence("events_id_seq"),
        primary_key=True,
        server_default=sa.text("nextval('events_id_seq')"),
    ),
    sa.Column("ts", TS, primary_key=True),
    _env(),
    sa.Column("category", _enum("event_category", *EVENT_CATEGORIES), nullable=False),
    sa.Column("message", sa.Text, nullable=False),
    sa.Column("symbol", sa.Text, nullable=True),
    sa.Column("correlation_id", sa.Uuid, nullable=True),
    sa.Column("refs", JSONB, nullable=True),
    sa.Index("ix_events_env_ts", "environment", "ts"),
    sa.Index("ix_events_env_category_ts", "environment", "category", "ts"),
    postgresql_partition_by="RANGE (ts)",
)

sa.Table(
    "news_items",
    metadata,
    _id(),
    sa.Column("dedupe_key", sa.Text, nullable=False, unique=True),
    sa.Column("sources", JSONB, nullable=False),
    sa.Column("headline", sa.Text, nullable=False),
    sa.Column("url", sa.Text, nullable=True),
    sa.Column("category", sa.Text, nullable=True),
    sa.Column("assets", JSONB, nullable=False),
    sa.Column("published_at", TS, nullable=False),
    sa.Column("impact_scheduled", sa.Boolean, nullable=False),
    sa.Column("assessment", JSONB, nullable=True),
    sa.Index("ix_news_items_published", "published_at"),
)

sa.Table(
    "news_provider_configs",
    metadata,
    _id(),
    sa.Column("provider", sa.Text, nullable=False, unique=True),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("credential_reference", sa.Text, nullable=True),
    sa.Column("created_at", TS, nullable=False),
    sa.Column("updated_at", TS, nullable=False),
    sa.CheckConstraint(
        "credential_reference IS NULL OR credential_reference LIKE 'vault:%'",
        name="credential_is_reference",
    ),
)

sa.Table(
    "backtest_runs",
    metadata,
    _id(),
    sa.Column("config", JSONB, nullable=False),
    sa.Column(
        "status",
        _enum("backtest_status", "queued", "running", "completed", "cancelled", "failed"),
        nullable=False,
    ),
    sa.Column("progress", MONEY, nullable=False),
    sa.Column("report", JSONB, nullable=True),
    sa.Column("created_at", TS, nullable=False),
    sa.Column("completed_at", TS, nullable=True),
)

ENSURE_PARTITIONS_FN = """
CREATE OR REPLACE FUNCTION mooo_ensure_month_partitions(
    parent_table text, months_back integer, months_ahead integer
) RETURNS integer
LANGUAGE plpgsql AS $fn$
DECLARE
    base_month date := date_trunc('month', timezone('UTC', now()))::date;
    month_start date;
    partition_name text;
    created integer := 0;
BEGIN
    FOR i IN (0 - months_back)..months_ahead LOOP
        month_start := (base_month + make_interval(months => i))::date;
        partition_name := parent_table || '_p' || to_char(month_start, 'YYYYMM');
        IF to_regclass(partition_name) IS NULL THEN
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                partition_name,
                parent_table,
                timezone('UTC', month_start::timestamp),
                timezone('UTC', (month_start + interval '1 month')::timestamp)
            );
            created := created + 1;
        END IF;
    END LOOP;
    RETURN created;
END
$fn$;
"""

DROP_EXPIRED_FN = """
CREATE OR REPLACE FUNCTION mooo_drop_expired_partitions(
    parent_table text, retention_days integer
) RETURNS integer
LANGUAGE plpgsql AS $fn$
DECLARE
    cutoff timestamptz;
    child record;
    upper_bound timestamptz;
    dropped integer := 0;
BEGIN
    IF retention_days < 180 THEN
        RAISE EXCEPTION 'retention_days must be at least 180, got %', retention_days;
    END IF;
    cutoff := now() - make_interval(days => retention_days);
    FOR child IN
        SELECT c.relname AS name
        FROM pg_inherits inh
        JOIN pg_class c ON c.oid = inh.inhrelid
        JOIN pg_class p ON p.oid = inh.inhparent
        WHERE p.relname = parent_table
          AND c.relname ~ ('^' || parent_table || '_p[0-9]{6}$')
    LOOP
        upper_bound := timezone(
            'UTC', (to_date(right(child.name, 6), 'YYYYMM') + interval '1 month')::timestamp
        );
        IF upper_bound <= cutoff THEN
            EXECUTE format('DROP TABLE %I', child.name);
            dropped := dropped + 1;
        END IF;
    END LOOP;
    RETURN dropped;
END
$fn$;
"""


def upgrade() -> None:
    bind = op.get_bind()
    metadata.create_all(bind)
    op.execute(ENSURE_PARTITIONS_FN)
    op.execute(DROP_EXPIRED_FN)
    for parent in PARTITIONED:
        op.execute(f"SELECT mooo_ensure_month_partitions('{parent}', 1, 3)")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS mooo_drop_expired_partitions(text, integer)")
    op.execute("DROP FUNCTION IF EXISTS mooo_ensure_month_partitions(text, integer, integer)")
    metadata.drop_all(op.get_bind())
