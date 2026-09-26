import sqlalchemy as sa

from mooo_core.models import PARTITIONED_TABLES, metadata

# Tables that are global by design (operator, singleton status, market data, run metadata).
GLOBAL_TABLES = {
    "operator_accounts",
    "agent_status",
    "live_enablements",
    "strategy_configs",
    "news_items",
    "news_provider_configs",
    "backtest_runs",
}
RISK_COLUMNS = {"minimum_liquidation_buffer", "regime_leverage_cap", "leverage_reduction_factor"}


def _columns() -> list[tuple[str, sa.Column[object]]]:
    return [(table.name, column) for table in metadata.tables.values() for column in table.c]


def test_every_trading_and_analysis_table_has_an_environment_column() -> None:
    missing = [
        name
        for name, table in metadata.tables.items()
        if name not in GLOBAL_TABLES and "environment" not in table.c
    ]
    assert missing == []


def test_timestamps_are_timezone_aware() -> None:
    naive = [
        f"{table}.{column.name}"
        for table, column in _columns()
        if isinstance(column.type, sa.DateTime) and not column.type.timezone
    ]
    assert naive == []


def test_money_columns_use_numeric_28_10() -> None:
    wrong = [
        f"{table}.{column.name}"
        for table, column in _columns()
        if isinstance(column.type, sa.Numeric)
        and not isinstance(column.type, sa.Float)
        and (column.type.precision, column.type.scale) != (28, 10)
    ]
    assert wrong == []


def test_credentials_have_no_plaintext_columns() -> None:
    table = metadata.tables["exchange_credentials"]
    names = set(table.c.keys())
    assert not names & {"api_key", "api_secret", "secret", "password", "master_key"}
    for name in ("api_key_ciphertext", "secret_ciphertext", "wrapped_data_key", "nonces"):
        assert isinstance(table.c[name].type, sa.LargeBinary)


def test_no_combined_score_or_rank_columns() -> None:
    offending = [
        f"{table}.{column.name}"
        for table, column in _columns()
        if "score" in column.name or column.name.endswith("rank")
    ]
    assert offending == []


def test_no_default_leverage_is_stored() -> None:
    offending = [
        f"{table}.{column.name}"
        for table, column in _columns()
        if "leverage" in column.name
        and ("default" in column.name or "target" in column.name)
    ]
    assert offending == []
    assert set(metadata.tables["risk_profiles"].c.keys()) >= RISK_COLUMNS


def test_events_and_snapshots_are_partitioned_by_range() -> None:
    for name in PARTITIONED_TABLES:
        options = metadata.tables[name].dialect_options["postgresql"]
        assert str(options["partition_by"]).startswith("RANGE")


def test_order_link_id_and_news_dedupe_are_unique() -> None:
    assert metadata.tables["orders"].c.order_link_id.unique
    assert metadata.tables["news_items"].c.dedupe_key.unique
    assert metadata.tables["news_provider_configs"].c.provider.unique
