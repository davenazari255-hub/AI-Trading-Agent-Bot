"""Repository tests against a migrated PostgreSQL database.

Each test runs inside a transaction that is rolled back. In CI the database URL is
required, so these tests fail instead of skipping when it is missing.
"""

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from mooo_core.models import (
    AgentState,
    DecisionOutcome,
    DecisionRecord,
    DeepAnalysisOutcome,
    DeepAnalysisTrigger,
    Environment,
    EventCategory,
    FillRecord,
    OrderRecord,
    OrderSide,
    TradeSide,
    WatchlistSource,
)
from mooo_core.repositories import (
    AgentRepository,
    AgentStatusRepository,
    DecisionRepository,
    DiscoveryRepository,
    DuplicateOrderIntentError,
    EventRepository,
    InvalidCredentialReferenceError,
    InvalidDecisionError,
    NewsRepository,
    OrderRepository,
    PartitionMaintenance,
    ProtectedWatchlistEntryError,
    RegimeRepository,
    RiskRepository,
)
from mooo_core.schemas import OpportunityDimension, OpportunityProfile, initial_risk_limits

NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    url = os.environ.get("MOOO_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail("MOOO_TEST_DATABASE_URL must be set in CI")
        pytest.skip("set MOOO_TEST_DATABASE_URL to a migrated database to run these tests")
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()


def _order(link: str) -> OrderRecord:
    return OrderRecord(
        order_link_id=link,
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type="Limit",
        qty=Decimal("0.001"),
        price=Decimal("60000"),
    )


def _decision(outcome: DecisionOutcome) -> DecisionRecord:
    return DecisionRecord(
        cycle_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        symbol="ETHUSDT",
        outcome=outcome,
        deep_analysis_trigger=DeepAnalysisTrigger.OPPORTUNITY_SIGNAL,
        model="test-model",
    )


async def test_events_are_isolated_by_environment(db_session: AsyncSession) -> None:
    demo = EventRepository(db_session, Environment.DEMO)
    live = EventRepository(db_session, Environment.LIVE)
    backtest = EventRepository(db_session, Environment.BACKTEST)
    marker = uuid.uuid4()
    await demo.append(EventCategory.INFO, "demo event", correlation_id=marker)
    await backtest.append(EventCategory.TRADE, "backtest event", correlation_id=marker)

    demo_events = [e.message for e in await demo.recent(500) if e.correlation_id == marker]
    live_events = [e.message for e in await live.recent(500) if e.correlation_id == marker]
    assert demo_events == ["demo event"]
    assert live_events == []
    assert all(e.environment == Environment.DEMO for e in await demo.recent(500))


async def test_events_get_increasing_ids_in_a_monthly_partition(db_session: AsyncSession) -> None:
    repo = EventRepository(db_session, Environment.DEMO)
    first = await repo.append(EventCategory.RISK, "one")
    second = await repo.append(EventCategory.RISK, "two")
    assert second.id > first.id
    result = await db_session.execute(
        text("SELECT tableoid::regclass::text FROM events WHERE id = :id"), {"id": first.id}
    )
    assert str(result.scalar_one()).startswith("events_p")


async def test_partition_retention_is_at_least_180_days(db_session: AsyncSession) -> None:
    maintenance = PartitionMaintenance(db_session)
    assert await maintenance.ensure_month_partitions() >= 0
    with pytest.raises(ValueError):
        await maintenance.drop_expired_partitions(30)
    assert await maintenance.drop_expired_partitions(180) == 0


async def test_risk_profiles_start_conservative_per_environment(db_session: AsyncSession) -> None:
    for environment in Environment:
        profile = await RiskRepository(db_session, environment).get_profile()
        assert profile.environment == environment
        assert profile.limits == initial_risk_limits().model_dump(mode="json")
        assert "default_leverage" not in profile.limits
        assert profile.minimum_liquidation_buffer == Decimal("2")
        assert profile.regime_leverage_cap == Decimal("3")
        assert profile.leverage_reduction_factor == Decimal("0.5")


async def test_risk_profile_rejects_regime_cap_above_max_leverage(
    db_session: AsyncSession,
) -> None:
    repo = RiskRepository(db_session, Environment.DEMO)
    with pytest.raises(ValueError):
        await repo.update_profile(
            initial_risk_limits(),
            minimum_liquidation_buffer=2,
            regime_leverage_cap=10,
            leverage_reduction_factor=0.5,
        )
    updated = await repo.update_profile(
        initial_risk_limits(),
        minimum_liquidation_buffer=2.5,
        regime_leverage_cap=2,
        leverage_reduction_factor=0.25,
    )
    assert updated.regime_leverage_cap == Decimal("2")


async def test_order_link_id_is_unique_and_orders_are_scoped(db_session: AsyncSession) -> None:
    repo = OrderRepository(db_session, Environment.DEMO)
    link = f"mooo-{uuid.uuid4().hex[:12]}"
    await repo.add_order(_order(link))
    with pytest.raises(DuplicateOrderIntentError):
        await repo.add_order(_order(link))
    assert await repo.get_by_link_id(link) is not None
    assert await OrderRepository(db_session, Environment.LIVE).get_by_link_id(link) is None

    fill = FillRecord(
        exec_id=f"exec-{uuid.uuid4().hex}",
        order_link_id=link,
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        price=Decimal("60000"),
        qty=Decimal("0.001"),
        fee=Decimal("0.036"),
        is_maker=False,
        exec_time=NOW,
    )
    assert await repo.record_fill(fill) is True
    assert await repo.record_fill(fill) is False
    assert len(await repo.fills_for(link)) == 1


async def test_watchlist_protects_anchor_and_open_position_entries(
    db_session: AsyncSession,
) -> None:
    repo = DiscoveryRepository(db_session, Environment.DEMO)
    await repo.admit("BTCUSDT", WatchlistSource.ANCHOR, "Permanent Anchor Instrument")
    await repo.admit("SOLUSDT", WatchlistSource.OPEN_POSITION, "open position")
    await repo.admit("DOGEUSDT", WatchlistSource.FAST_SCANNER, "volume expansion")

    with pytest.raises(ProtectedWatchlistEntryError):
        await repo.remove("BTCUSDT", idle_expiry=False)
    with pytest.raises(ProtectedWatchlistEntryError):
        await repo.remove("SOLUSDT", idle_expiry=True)
    assert await repo.remove("DOGEUSDT", idle_expiry=True) is True

    assert {e.symbol for e in await repo.active_watchlist()} == {"BTCUSDT", "SOLUSDT"}
    assert await DiscoveryRepository(db_session, Environment.LIVE).active_watchlist() == []


async def test_watchlist_has_no_fixed_size_and_admission_is_idempotent(
    db_session: AsyncSession,
) -> None:
    repo = DiscoveryRepository(db_session, Environment.DEMO)
    for index in range(30):
        await repo.admit(f"TEST{index}USDT", WatchlistSource.FAST_SCANNER, "signal")
    again = await repo.admit("TEST0USDT", WatchlistSource.AGENT, "agent request")
    assert again.source == WatchlistSource.FAST_SCANNER
    assert len(await repo.active_watchlist()) == 30

    assert await repo.remove("TEST1USDT", idle_expiry=True) is True
    readmitted = await repo.admit("TEST1USDT", WatchlistSource.AGENT, "agent request")
    assert readmitted.source == WatchlistSource.AGENT


async def test_opportunity_profile_snapshot_round_trip(db_session: AsyncSession) -> None:
    repo = DiscoveryRepository(db_session, Environment.DEMO)
    profile = OpportunityProfile(
        dimensions={
            "liquidity": OpportunityDimension(
                measurements={"turnover_1h_usdt": 3_500_000.0},
                label="High liquidity",
                data_as_of=NOW,
            ),
            "volatility": OpportunityDimension(
                measurements={"atr_1h_pct": 0.8}, label="Expanding", data_as_of=NOW
            ),
        }
    )
    await repo.save_profile("ETHUSDT", profile, captured_at=NOW)
    later = await repo.save_profile("ETHUSDT", profile, captured_at=NOW + timedelta(minutes=1))

    latest = await repo.latest_profile("ETHUSDT")
    assert latest is not None
    assert latest.id == later.id
    assert set(latest.dimensions) == {"liquidity", "volatility"}
    assert latest.dimensions["liquidity"]["label"] == "High liquidity"
    assert await DiscoveryRepository(db_session, Environment.LIVE).latest_profile("ETHUSDT") is None


async def test_funnel_counts_and_discovery_settings(db_session: AsyncSession) -> None:
    repo = DiscoveryRepository(db_session, Environment.DEMO)
    funnel = await repo.record_funnel(uuid.uuid4(), {"universe": 400, "liquid": 120, "hot": 6})
    assert funnel.stage_counts["hot"] == 6
    with pytest.raises(ValidationError):
        await repo.record_funnel(uuid.uuid4(), {"universe": -1})

    settings = await repo.get_settings()
    assert settings.watchlist_capacity == 20
    with pytest.raises(ValueError):
        await DiscoveryRepository(db_session, Environment.BACKTEST).get_settings()


async def test_deep_analysis_runs_record_usage_and_budget(db_session: AsyncSession) -> None:
    repo = AgentRepository(db_session, Environment.DEMO)
    settings = await repo.get_settings()
    assert (
        settings.ai_analysis_budget,
        settings.deep_analysis_cooldown_seconds,
        settings.anchor_review_interval_seconds,
    ) == (40, 900, 3600)

    since = datetime.now(UTC) - timedelta(seconds=5)
    run = await repo.start_deep_analysis(
        correlation_id=uuid.uuid4(),
        trigger_type=DeepAnalysisTrigger.OPPORTUNITY_SIGNAL.value,
        instruments=["ETHUSDT"],
        model="test-model",
    )
    await repo.start_deep_analysis(
        correlation_id=uuid.uuid4(),
        trigger_type="thesis_check",
        instruments=["ETHUSDT"],
        model="test-model",
        is_thesis_check=True,
    )
    done = await repo.complete_deep_analysis(
        run.id,
        outcome=DeepAnalysisOutcome.NO_TRADE,
        input_tokens=1200,
        output_tokens=300,
        duration_ms=4200,
    )
    assert done.completed_at is not None
    assert (done.input_tokens, done.output_tokens, done.duration_ms) == (1200, 300, 4200)
    assert await repo.count_new_trade_analyses_since(since) == 1
    assert await AgentRepository(db_session, Environment.LIVE).count_new_trade_analyses_since(
        since
    ) == 0


async def test_agent_status_starts_in_demo_and_stopped(db_session: AsyncSession) -> None:
    repo = AgentStatusRepository(db_session)
    status = await repo.get()
    assert status.active_environment == Environment.DEMO.value
    assert status.state == AgentState.STOPPED
    halted = await repo.set_state(
        AgentState.HALTED, halt_reason="kill switch", halt_details={"trigger": "daily_loss"}
    )
    assert halted.halted_at is not None
    resumed = await repo.set_state(AgentState.STOPPED)
    assert resumed.halt_reason is None


async def test_regime_transitions_are_recorded(db_session: AsyncSession) -> None:
    repo = RegimeRepository(db_session, Environment.DEMO)
    await repo.record_transition("BTCUSDT", from_regime=None, to_regime="trending_up", detected_at=NOW)
    await repo.record_transition(
        "BTCUSDT",
        from_regime="trending_up",
        to_regime="range",
        detected_at=NOW + timedelta(minutes=5),
    )
    recent = await repo.recent_transitions("BTCUSDT")
    assert [t.to_regime for t in recent] == ["range", "trending_up"]
    assert await RegimeRepository(db_session, Environment.LIVE).recent_transitions("BTCUSDT") == []


async def test_news_provider_credentials_are_references_only(db_session: AsyncSession) -> None:
    repo = NewsRepository(db_session)
    with pytest.raises(InvalidCredentialReferenceError):
        await repo.upsert_provider("cryptopanic", enabled=True, credential_reference="plain-key")
    await repo.upsert_provider(
        "CryptoPanic", enabled=True, credential_reference="vault:news/cryptopanic"
    )
    await repo.upsert_provider("rss", enabled=False)
    enabled = await repo.enabled_providers()
    assert "cryptopanic" in enabled
    assert "rss" not in enabled

    updated = await repo.upsert_provider("cryptopanic", enabled=False)
    assert updated.enabled is False

    key = f"news-{uuid.uuid4().hex}"
    item = {
        "dedupe_key": key,
        "headline": "Exchange lists new perpetual",
        "sources": ["rss"],
        "assets": ["ETH"],
        "published_at": NOW,
    }
    assert await repo.add_item(**item) is True
    assert await repo.add_item(**item) is False


async def test_decisions_need_evidence_and_are_scoped(db_session: AsyncSession) -> None:
    repo = DecisionRepository(db_session, Environment.DEMO)
    with pytest.raises(InvalidDecisionError):
        await repo.add_decision(_decision(DecisionOutcome.TRADE_PROPOSED))
    with pytest.raises(InvalidDecisionError):
        await repo.add_decision(_decision(DecisionOutcome.NO_TRADE))

    snapshot = await repo.save_analysis_snapshot(
        "ETHUSDT",
        features={"rsi_1h": 55},
        regimes={"1h": "trending_up"},
        market_context={},
        data_as_of=NOW,
    )
    record = _decision(DecisionOutcome.TRADE_PROPOSED)
    record.side = TradeSide.LONG
    record.proposed_leverage = Decimal("2")
    record.leverage_rationale = {"atr_1h_pct": 0.8, "stop_distance_pct": 1.1}
    record.analysis_snapshot_id = snapshot.id
    stored = await repo.add_decision(record)

    assert await repo.get(stored.id) is not None
    assert await DecisionRepository(db_session, Environment.LIVE).get(stored.id) is None
