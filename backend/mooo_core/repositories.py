"""Async repositories over the Mooo schema.

Environment-scoped repositories take the environment once, at construction, and add
``environment = :env`` to every query and every row they write. Backtest rows use
environment ``backtest`` and are never returned by a Demo or Live repository.
Repositories flush but never commit. The caller owns the transaction.
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mooo_core.models import (
    PARTITIONED_TABLES,
    AccountEnvironment,
    AgentSettings,
    AgentState,
    AgentStatusRecord,
    AnalysisSnapshot,
    BacktestRun,
    BacktestStatus,
    DecisionOutcome,
    DecisionRecord,
    DeepAnalysisOutcome,
    DeepAnalysisRun,
    DiscoveryFunnel,
    DiscoverySettings,
    DynamicWatchlistEntry,
    Environment,
    EquitySnapshot,
    EventCategory,
    EventRecord,
    FillRecord,
    NewsItem,
    NewsProviderConfig,
    OpportunityProfileSnapshot,
    OrderRecord,
    OrderStatus,
    PositionRecord,
    PositionStatus,
    RegimeTransition,
    RiskOutcome,
    RiskProfile,
    RiskVerdictRecord,
    WatchlistSource,
    utcnow,
)
from mooo_core.schemas import (
    DiscoverySettingsSchema,
    FunnelStageCounts,
    OpportunityProfile,
    RiskCheck,
    RiskLimits,
    initial_agent_settings,
    initial_discovery_settings,
    initial_risk_columns,
    initial_risk_limits,
)

MIN_PARTITION_RETENTION_DAYS = 180
CREDENTIAL_REFERENCE_PREFIX = "vault:"

_FUNNEL_COUNTS: TypeAdapter[FunnelStageCounts] = TypeAdapter(FunnelStageCounts)


class RepositoryError(Exception):
    """Base class for repository rule violations."""


class DuplicateOrderIntentError(RepositoryError):
    """An order with the same orderLinkId already exists."""


class ProtectedWatchlistEntryError(RepositoryError):
    """Anchor and open-position entries cannot be removed this way."""


class InvalidCredentialReferenceError(RepositoryError):
    """A news provider credential must be a vault reference, never a plaintext value."""


class InvalidDecisionError(RepositoryError):
    """A decision record is missing required evidence."""


def _decimal(value: float | int | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _required_decimal(value: float | int | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def account_environment(environment: Environment) -> AccountEnvironment:
    """Settings exist for Demo and Live only."""
    if environment is Environment.BACKTEST:
        raise ValueError("settings exist only for the demo and live environments")
    return AccountEnvironment(environment.value)


class EnvironmentScopedRepository:
    def __init__(self, session: AsyncSession, environment: Environment | str) -> None:
        self.session = session
        self.environment = Environment(environment)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class DiscoveryRepository(EnvironmentScopedRepository):
    """Dynamic Watchlist, Opportunity Profile snapshots, funnels, and Discovery Settings."""

    async def active_entry(self, symbol: str) -> DynamicWatchlistEntry | None:
        stmt = select(DynamicWatchlistEntry).where(
            DynamicWatchlistEntry.environment == self.environment,
            DynamicWatchlistEntry.symbol == symbol,
            DynamicWatchlistEntry.removed_at.is_(None),
        )
        return (await self.session.scalars(stmt)).one_or_none()

    async def active_watchlist(self) -> list[DynamicWatchlistEntry]:
        stmt = (
            select(DynamicWatchlistEntry)
            .where(
                DynamicWatchlistEntry.environment == self.environment,
                DynamicWatchlistEntry.removed_at.is_(None),
            )
            .order_by(DynamicWatchlistEntry.admitted_at, DynamicWatchlistEntry.symbol)
        )
        return list((await self.session.scalars(stmt)).all())

    async def admit(
        self,
        symbol: str,
        source: WatchlistSource,
        attention_reason: str,
        *,
        at: datetime | None = None,
    ) -> DynamicWatchlistEntry:
        """Admit an Instrument. Admitting an already active Instrument returns its entry."""
        existing = await self.active_entry(symbol)
        if existing is not None:
            return existing
        entry = DynamicWatchlistEntry(
            environment=self.environment,
            symbol=symbol,
            source=source,
            attention_reason=attention_reason,
            admitted_at=at or utcnow(),
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def record_opportunity_signal(self, symbol: str, *, at: datetime | None = None) -> bool:
        entry = await self.active_entry(symbol)
        if entry is None:
            return False
        entry.last_opportunity_signal_at = at or utcnow()
        await self.session.flush()
        return True

    async def remove(self, symbol: str, *, idle_expiry: bool, at: datetime | None = None) -> bool:
        """Remove an entry. Anchors are permanent. Open positions never expire by idleness."""
        entry = await self.active_entry(symbol)
        if entry is None:
            return False
        if entry.source == WatchlistSource.ANCHOR:
            raise ProtectedWatchlistEntryError(f"{symbol} is a Permanent Anchor Instrument")
        if idle_expiry and entry.source == WatchlistSource.OPEN_POSITION:
            raise ProtectedWatchlistEntryError(f"{symbol} has an open position")
        entry.removed_at = at or utcnow()
        await self.session.flush()
        return True

    async def save_profile(
        self,
        symbol: str,
        profile: OpportunityProfile,
        *,
        captured_at: datetime | None = None,
        source_profile_id: uuid.UUID | None = None,
    ) -> OpportunityProfileSnapshot:
        snapshot = OpportunityProfileSnapshot(
            environment=self.environment,
            symbol=symbol,
            dimensions=profile.model_dump(mode="json")["dimensions"],
            source_profile_id=source_profile_id,
            captured_at=captured_at or utcnow(),
        )
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot

    async def latest_profile(self, symbol: str) -> OpportunityProfileSnapshot | None:
        stmt = (
            select(OpportunityProfileSnapshot)
            .where(
                OpportunityProfileSnapshot.environment == self.environment,
                OpportunityProfileSnapshot.symbol == symbol,
            )
            .order_by(OpportunityProfileSnapshot.captured_at.desc())
            .limit(1)
        )
        return (await self.session.scalars(stmt)).first()

    async def record_funnel(
        self,
        sweep_id: uuid.UUID,
        stage_counts: Mapping[str, int],
        *,
        completed_at: datetime | None = None,
    ) -> DiscoveryFunnel:
        counts = _FUNNEL_COUNTS.validate_python(dict(stage_counts))
        funnel = DiscoveryFunnel(
            environment=self.environment,
            sweep_id=sweep_id,
            stage_counts=dict(counts),
            completed_at=completed_at or utcnow(),
        )
        self.session.add(funnel)
        await self.session.flush()
        return funnel

    async def get_settings(self) -> DiscoverySettingsSchema:
        row = await self._settings_row()
        return DiscoverySettingsSchema.model_validate(row.values)

    async def update_settings(self, settings: DiscoverySettingsSchema) -> None:
        row = await self._settings_row()
        row.values = settings.model_dump(mode="json")
        row.updated_at = utcnow()
        await self.session.flush()

    async def _settings_row(self) -> DiscoverySettings:
        env = account_environment(self.environment)
        stmt = (
            pg_insert(DiscoverySettings)
            .values(
                {
                    "environment": env,
                    "values": initial_discovery_settings().model_dump(mode="json"),
                    "updated_at": utcnow(),
                }
            )
            .on_conflict_do_nothing(index_elements=["environment"])
        )
        await self.session.execute(stmt)
        row = await self.session.get(DiscoverySettings, env)
        if row is None:
            raise RepositoryError("discovery settings row is missing")
        return row


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class AgentStatusRepository:
    """The single agent status row. A new row starts in Demo and stopped."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self) -> AgentStatusRecord:
        stmt = (
            pg_insert(AgentStatusRecord)
            .values(
                {
                    "id": 1,
                    "active_environment": AccountEnvironment.DEMO,
                    "state": AgentState.STOPPED,
                    "updated_at": utcnow(),
                }
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        await self.session.execute(stmt)
        row = await self.session.get(AgentStatusRecord, 1)
        if row is None:
            raise RepositoryError("agent status row is missing")
        return row

    async def set_state(
        self,
        state: AgentState,
        *,
        halt_reason: str | None = None,
        halt_details: Mapping[str, Any] | None = None,
    ) -> AgentStatusRecord:
        row = await self.get()
        now = utcnow()
        row.state = state
        if state == AgentState.HALTED:
            row.halt_reason = halt_reason
            row.halt_details = dict(halt_details) if halt_details is not None else None
            row.halted_at = now
        else:
            row.halt_reason = None
            row.halt_details = None
            row.halted_at = None
        row.updated_at = now
        await self.session.flush()
        return row


class AgentRepository(EnvironmentScopedRepository):
    """Agent Settings and Deep Analysis run accounting."""

    async def get_settings(self) -> AgentSettings:
        env = account_environment(self.environment)
        stmt = (
            pg_insert(AgentSettings)
            .values({"environment": env, **initial_agent_settings(), "updated_at": utcnow()})
            .on_conflict_do_nothing(index_elements=["environment"])
        )
        await self.session.execute(stmt)
        row = await self.session.get(AgentSettings, env)
        if row is None:
            raise RepositoryError("agent settings row is missing")
        return row

    async def update_settings(
        self,
        *,
        ai_analysis_budget: int,
        deep_analysis_cooldown_seconds: int,
        anchor_review_interval_seconds: int,
    ) -> AgentSettings:
        values = (
            ai_analysis_budget,
            deep_analysis_cooldown_seconds,
            anchor_review_interval_seconds,
        )
        if min(values) <= 0:
            raise ValueError("agent settings must be positive")
        row = await self.get_settings()
        row.ai_analysis_budget = ai_analysis_budget
        row.deep_analysis_cooldown_seconds = deep_analysis_cooldown_seconds
        row.anchor_review_interval_seconds = anchor_review_interval_seconds
        row.updated_at = utcnow()
        await self.session.flush()
        return row

    async def start_deep_analysis(
        self,
        *,
        correlation_id: uuid.UUID,
        trigger_type: str,
        instruments: Sequence[str],
        model: str,
        is_thesis_check: bool = False,
        started_at: datetime | None = None,
    ) -> DeepAnalysisRun:
        run = DeepAnalysisRun(
            environment=self.environment,
            correlation_id=correlation_id,
            trigger_type=trigger_type,
            is_thesis_check=is_thesis_check,
            instruments=list(instruments),
            model=model,
            input_tokens=0,
            output_tokens=0,
            started_at=started_at or utcnow(),
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def complete_deep_analysis(
        self,
        run_id: uuid.UUID,
        *,
        outcome: DeepAnalysisOutcome,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int,
        estimated_cost: Decimal | float | None = None,
        completed_at: datetime | None = None,
    ) -> DeepAnalysisRun:
        stmt = select(DeepAnalysisRun).where(
            DeepAnalysisRun.id == run_id, DeepAnalysisRun.environment == self.environment
        )
        run = (await self.session.scalars(stmt)).one()
        run.outcome = outcome
        run.input_tokens = input_tokens
        run.output_tokens = output_tokens
        run.duration_ms = duration_ms
        run.estimated_cost = _decimal(estimated_cost)
        run.completed_at = completed_at or utcnow()
        await self.session.flush()
        return run

    async def count_new_trade_analyses_since(self, since: datetime) -> int:
        """Runs that count against the AI Analysis Budget. Thesis checks are excluded."""
        stmt = (
            select(func.count())
            .select_from(DeepAnalysisRun)
            .where(
                DeepAnalysisRun.environment == self.environment,
                DeepAnalysisRun.started_at >= since,
                DeepAnalysisRun.is_thesis_check.is_(False),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())


# ---------------------------------------------------------------------------
# Regimes
# ---------------------------------------------------------------------------


class RegimeRepository(EnvironmentScopedRepository):
    async def record_transition(
        self,
        symbol: str,
        *,
        from_regime: str | None,
        to_regime: str,
        detected_at: datetime | None = None,
        source_event_id: uuid.UUID | None = None,
    ) -> RegimeTransition:
        transition = RegimeTransition(
            environment=self.environment,
            symbol=symbol,
            from_regime=from_regime,
            to_regime=to_regime,
            detected_at=detected_at or utcnow(),
            source_event_id=source_event_id,
        )
        self.session.add(transition)
        await self.session.flush()
        return transition

    async def recent_transitions(self, symbol: str, limit: int = 20) -> list[RegimeTransition]:
        stmt = (
            select(RegimeTransition)
            .where(
                RegimeTransition.environment == self.environment,
                RegimeTransition.symbol == symbol,
            )
            .order_by(RegimeTransition.detected_at.desc())
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------


class RiskRepository(EnvironmentScopedRepository):
    """Risk Profile per environment. A new profile starts with initial conservative values."""

    async def get_profile(self) -> RiskProfile:
        columns = initial_risk_columns()
        stmt = (
            pg_insert(RiskProfile)
            .values(
                {
                    "environment": self.environment,
                    "limits": initial_risk_limits().model_dump(mode="json"),
                    "minimum_liquidation_buffer": _required_decimal(
                        columns["minimum_liquidation_buffer"]
                    ),
                    "regime_leverage_cap": _decimal(columns["regime_leverage_cap"]),
                    "leverage_reduction_factor": _decimal(columns["leverage_reduction_factor"]),
                    "updated_at": utcnow(),
                }
            )
            .on_conflict_do_nothing(index_elements=["environment"])
        )
        await self.session.execute(stmt)
        row = await self.session.get(RiskProfile, self.environment)
        if row is None:
            raise RepositoryError("risk profile row is missing")
        return row

    async def get_limits(self) -> RiskLimits:
        return RiskLimits.model_validate((await self.get_profile()).limits)

    async def update_profile(
        self,
        limits: RiskLimits,
        *,
        minimum_liquidation_buffer: Decimal | float,
        regime_leverage_cap: Decimal | float | None,
        leverage_reduction_factor: Decimal | float | None,
    ) -> RiskProfile:
        buffer = _required_decimal(minimum_liquidation_buffer)
        cap = _decimal(regime_leverage_cap)
        factor = _decimal(leverage_reduction_factor)
        if buffer <= 0:
            raise ValueError("minimum liquidation buffer must be positive")
        if cap is not None and not Decimal(1) <= cap <= _required_decimal(limits.max_leverage):
            raise ValueError("regime leverage cap must be between 1 and the maximum leverage")
        if factor is not None and not Decimal(0) < factor <= Decimal(1):
            raise ValueError("leverage reduction factor must be in (0, 1]")
        row = await self.get_profile()
        row.limits = limits.model_dump(mode="json")
        row.minimum_liquidation_buffer = buffer
        row.regime_leverage_cap = cap
        row.leverage_reduction_factor = factor
        row.updated_at = utcnow()
        await self.session.flush()
        return row

    async def record_verdict(
        self,
        *,
        proposal: Mapping[str, Any],
        outcome: RiskOutcome,
        checks: Sequence[RiskCheck],
        engine_version: str,
        adjustments: Mapping[str, Any] | None = None,
    ) -> RiskVerdictRecord:
        verdict = RiskVerdictRecord(
            environment=self.environment,
            proposal=dict(proposal),
            outcome=outcome,
            checks=[check.model_dump(mode="json") for check in checks],
            adjustments=dict(adjustments) if adjustments is not None else None,
            engine_version=engine_version,
        )
        self.session.add(verdict)
        await self.session.flush()
        return verdict


# ---------------------------------------------------------------------------
# Decisions and analysis snapshots
# ---------------------------------------------------------------------------


class DecisionRepository(EnvironmentScopedRepository):
    async def save_analysis_snapshot(
        self,
        symbol: str,
        *,
        features: Mapping[str, Any],
        regimes: Mapping[str, Any],
        market_context: Mapping[str, Any],
        data_as_of: datetime,
    ) -> AnalysisSnapshot:
        snapshot = AnalysisSnapshot(
            environment=self.environment,
            symbol=symbol,
            features=dict(features),
            regimes=dict(regimes),
            market_context=dict(market_context),
            data_as_of=data_as_of,
        )
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot

    async def add_decision(self, record: DecisionRecord) -> DecisionRecord:
        """Store a decision. Trade proposals need side, leverage, and a leverage rationale."""
        if record.outcome == DecisionOutcome.TRADE_PROPOSED and (
            record.side is None
            or record.proposed_leverage is None
            or not record.leverage_rationale
        ):
            raise InvalidDecisionError("trade proposals need side, leverage, and its rationale")
        if record.outcome == DecisionOutcome.NO_TRADE and not record.no_trade_reason:
            raise InvalidDecisionError("NO TRADE decisions need a reason")
        record.environment = self.environment
        self.session.add(record)
        await self.session.flush()
        return record

    async def get(self, decision_id: uuid.UUID) -> DecisionRecord | None:
        stmt = select(DecisionRecord).where(
            DecisionRecord.id == decision_id, DecisionRecord.environment == self.environment
        )
        return (await self.session.scalars(stmt)).one_or_none()

    async def recent(self, limit: int = 50) -> list[DecisionRecord]:
        stmt = (
            select(DecisionRecord)
            .where(DecisionRecord.environment == self.environment)
            .order_by(DecisionRecord.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())


# ---------------------------------------------------------------------------
# Orders, fills, positions, equity
# ---------------------------------------------------------------------------


class OrderRepository(EnvironmentScopedRepository):
    async def add_order(self, order: OrderRecord) -> OrderRecord:
        """Persist an order intent before submission. The orderLinkId is unique."""
        order.environment = self.environment
        try:
            async with self.session.begin_nested():
                self.session.add(order)
                await self.session.flush()
        except IntegrityError as exc:
            raise DuplicateOrderIntentError(order.order_link_id) from exc
        return order

    async def get_by_link_id(self, order_link_id: str) -> OrderRecord | None:
        stmt = select(OrderRecord).where(
            OrderRecord.order_link_id == order_link_id,
            OrderRecord.environment == self.environment,
        )
        return (await self.session.scalars(stmt)).one_or_none()

    async def update_status(
        self,
        order_link_id: str,
        status: OrderStatus,
        *,
        exchange_order_id: str | None = None,
        reject_reason: str | None = None,
    ) -> OrderRecord:
        order = await self.get_by_link_id(order_link_id)
        if order is None:
            raise RepositoryError(f"unknown order {order_link_id}")
        order.status = status
        if exchange_order_id is not None:
            order.exchange_order_id = exchange_order_id
        if reject_reason is not None:
            order.reject_reason = reject_reason
        order.updated_at = utcnow()
        await self.session.flush()
        return order

    async def open_orders(self) -> list[OrderRecord]:
        terminal = (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)
        stmt = select(OrderRecord).where(
            OrderRecord.environment == self.environment, OrderRecord.status.not_in(terminal)
        )
        return list((await self.session.scalars(stmt)).all())

    async def record_fill(self, fill: FillRecord) -> bool:
        """Store a fill once. Returns False when the exec id was already stored."""
        stmt = (
            pg_insert(FillRecord)
            .values(
                {
                    "exec_id": fill.exec_id,
                    "environment": self.environment,
                    "order_link_id": fill.order_link_id,
                    "symbol": fill.symbol,
                    "side": fill.side,
                    "price": fill.price,
                    "qty": fill.qty,
                    "fee": fill.fee,
                    "fee_currency": fill.fee_currency,
                    "is_maker": fill.is_maker,
                    "exec_time": fill.exec_time,
                }
            )
            .on_conflict_do_nothing(index_elements=["exec_id"])
            .returning(FillRecord.exec_id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None

    async def fills_for(self, order_link_id: str) -> list[FillRecord]:
        stmt = select(FillRecord).where(
            FillRecord.environment == self.environment, FillRecord.order_link_id == order_link_id
        )
        return list((await self.session.scalars(stmt)).all())


class PositionRepository(EnvironmentScopedRepository):
    async def add_position(self, position: PositionRecord) -> PositionRecord:
        position.environment = self.environment
        self.session.add(position)
        await self.session.flush()
        return position

    async def open_positions(self) -> list[PositionRecord]:
        stmt = select(PositionRecord).where(
            PositionRecord.environment == self.environment,
            PositionRecord.status == PositionStatus.OPEN,
        )
        return list((await self.session.scalars(stmt)).all())

    async def record_equity(self, snapshot: EquitySnapshot) -> EquitySnapshot:
        snapshot.environment = self.environment
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot

    async def latest_equity(self) -> EquitySnapshot | None:
        stmt = (
            select(EquitySnapshot)
            .where(EquitySnapshot.environment == self.environment)
            .order_by(EquitySnapshot.ts.desc())
            .limit(1)
        )
        return (await self.session.scalars(stmt)).first()


# ---------------------------------------------------------------------------
# Events and partitions
# ---------------------------------------------------------------------------


class EventRepository(EnvironmentScopedRepository):
    async def append(
        self,
        category: EventCategory,
        message: str,
        *,
        symbol: str | None = None,
        correlation_id: uuid.UUID | None = None,
        refs: Mapping[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> EventRecord:
        event = EventRecord(
            environment=self.environment,
            category=category,
            message=message,
            symbol=symbol,
            correlation_id=correlation_id,
            refs=dict(refs) if refs is not None else None,
            ts=ts or utcnow(),
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def recent(
        self, limit: int = 100, *, category: EventCategory | None = None
    ) -> list[EventRecord]:
        stmt = select(EventRecord).where(EventRecord.environment == self.environment)
        if category is not None:
            stmt = stmt.where(EventRecord.category == category)
        stmt = stmt.order_by(EventRecord.ts.desc(), EventRecord.id.desc()).limit(limit)
        return list((await self.session.scalars(stmt)).all())


class PartitionMaintenance:
    """Creates monthly partitions ahead of time and drops partitions past retention."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def ensure_month_partitions(self, *, months_back: int = 1, months_ahead: int = 3) -> int:
        created = 0
        for parent in PARTITIONED_TABLES:
            result = await self.session.execute(
                text("SELECT mooo_ensure_month_partitions(:parent, :back, :ahead)"),
                {"parent": parent, "back": months_back, "ahead": months_ahead},
            )
            created += int(result.scalar_one())
        return created

    async def drop_expired_partitions(
        self, retention_days: int = MIN_PARTITION_RETENTION_DAYS
    ) -> int:
        if retention_days < MIN_PARTITION_RETENTION_DAYS:
            raise ValueError(
                f"retention must be at least {MIN_PARTITION_RETENTION_DAYS} days, "
                f"got {retention_days}"
            )
        dropped = 0
        for parent in PARTITIONED_TABLES:
            result = await self.session.execute(
                text("SELECT mooo_drop_expired_partitions(:parent, :days)"),
                {"parent": parent, "days": retention_days},
            )
            dropped += int(result.scalar_one())
        return dropped


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------


class NewsRepository:
    """News items and per-provider configuration. News is market data, not per environment."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_provider(
        self, provider: str, *, enabled: bool, credential_reference: str | None = None
    ) -> NewsProviderConfig:
        key = provider.strip().lower()
        if not key:
            raise ValueError("provider name is required")
        if credential_reference is not None and not credential_reference.startswith(
            CREDENTIAL_REFERENCE_PREFIX
        ):
            raise InvalidCredentialReferenceError(
                "news provider credentials must be stored as a vault reference"
            )
        now = utcnow()
        stmt = pg_insert(NewsProviderConfig).values(
            {
                "provider": key,
                "enabled": enabled,
                "credential_reference": credential_reference,
                "created_at": now,
                "updated_at": now,
            }
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["provider"],
            set_={
                "enabled": stmt.excluded.enabled,
                "credential_reference": stmt.excluded.credential_reference,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await self.session.execute(stmt)
        result = await self.session.scalars(
            select(NewsProviderConfig)
            .where(NewsProviderConfig.provider == key)
            .execution_options(populate_existing=True)
        )
        return result.one()

    async def enabled_providers(self) -> list[str]:
        stmt = (
            select(NewsProviderConfig.provider)
            .where(NewsProviderConfig.enabled.is_(True))
            .order_by(NewsProviderConfig.provider)
        )
        return list((await self.session.scalars(stmt)).all())

    async def add_item(
        self,
        *,
        dedupe_key: str,
        headline: str,
        sources: Sequence[str],
        assets: Sequence[str],
        published_at: datetime,
        url: str | None = None,
        category: str | None = None,
        impact_scheduled: bool = False,
    ) -> bool:
        """Store a news item once per dedupe key. Returns False for a duplicate."""
        stmt = (
            pg_insert(NewsItem)
            .values(
                {
                    "id": uuid.uuid4(),
                    "dedupe_key": dedupe_key,
                    "headline": headline,
                    "sources": list(sources),
                    "assets": list(assets),
                    "published_at": published_at,
                    "url": url,
                    "category": category,
                    "impact_scheduled": impact_scheduled,
                }
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(NewsItem.id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Backtests
# ---------------------------------------------------------------------------


class BacktestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_run(self, config: Mapping[str, Any]) -> BacktestRun:
        run = BacktestRun(config=dict(config), status=BacktestStatus.QUEUED, progress=Decimal(0))
        self.session.add(run)
        await self.session.flush()
        return run

    async def update_run(
        self,
        run_id: uuid.UUID,
        *,
        status: BacktestStatus,
        progress: Decimal | float | None = None,
        report: Mapping[str, Any] | None = None,
    ) -> BacktestRun:
        run = await self.session.get(BacktestRun, run_id)
        if run is None:
            raise RepositoryError(f"unknown backtest run {run_id}")
        run.status = status
        if progress is not None:
            run.progress = _required_decimal(progress)
        if report is not None:
            run.report = dict(report)
        if status in (BacktestStatus.COMPLETED, BacktestStatus.CANCELLED, BacktestStatus.FAILED):
            run.completed_at = utcnow()
        await self.session.flush()
        return run
