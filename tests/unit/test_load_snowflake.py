"""The loader is the only part of Assay that writes, so its guards are tested."""

from __future__ import annotations

import pytest

from demo import data, load_snowflake
from tests.unit.test_snowflake_adapter import ENV, FakeConnector


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)


def _statements(connector: FakeConnector) -> list[str]:
    return [sql for cursor in connector.connection.cursors for sql, _ in cursor.executed]


def test_a_dry_run_writes_nothing(capsys):
    assert load_snowflake.main(["--database", "ASSAY_DEMO", "--schema", "DEMO"]) == 0
    assert "Dry run" in capsys.readouterr().out


def test_the_plan_names_the_target_and_the_damage(capsys):
    load_snowflake.main(["--database", "ASSAY_DEMO", "--schema", "DEMO", "--days", "30"])
    out = capsys.readouterr().out
    assert "ASSAY_DEMO.DEMO" in out
    assert "DROP and recreate 7 tables" in out


def test_an_injected_identifier_is_refused():
    with pytest.raises(ValueError, match="not a bare identifier"):
        load_snowflake._qualify("ASSAY_DEMO", "DEMO; DROP DATABASE PROD")


def test_loading_creates_the_schema_before_using_it():
    connector = FakeConnector(rows=[[1]])
    load_snowflake.load("ASSAY_DEMO", "DEMO", days=2, connector=connector)
    statements = _statements(connector)
    assert statements[0].startswith("CREATE SCHEMA IF NOT EXISTS ASSAY_DEMO.DEMO")
    assert statements[1].startswith("USE SCHEMA ASSAY_DEMO.DEMO")


def test_every_table_is_dropped_and_recreated():
    connector = FakeConnector(rows=[[1]])
    load_snowflake.load("ASSAY_DEMO", "DEMO", days=2, connector=connector)
    statements = _statements(connector)
    for table in data.TABLES:
        assert f"DROP TABLE IF EXISTS {table}" in statements
        assert any(s.startswith(f"CREATE TABLE {table} (") for s in statements)


def test_row_counts_are_reported_per_table():
    connector = FakeConnector(rows=[[1]])
    written = load_snowflake.load("ASSAY_DEMO", "DEMO", days=2, connector=connector)
    assert set(written) == set(data.TABLES)
    assert written["events"] == 2 * data.EVENTS_PER_DAY


def test_events_use_snowflake_generator_not_generate_series():
    connector = FakeConnector(rows=[[1]])
    load_snowflake.load("ASSAY_DEMO", "DEMO", days=2, connector=connector)
    events = [s for s in _statements(connector) if "INSERT INTO events" in s][0]
    assert "TABLE(GENERATOR(ROWCOUNT => 920))" in events
    assert "generate_series" not in events


def test_seq4_is_aliased_once_rather_than_referenced_repeatedly():
    """Snowflake does not guarantee a stable SEQ4() across references in a row."""
    connector = FakeConnector(rows=[[1]])
    load_snowflake.load("ASSAY_DEMO", "DEMO", days=2, connector=connector)
    events = [s for s in _statements(connector) if "INSERT INTO events" in s][0]
    assert events.count("SEQ4()") == 1


def test_the_bind_style_is_switched_before_connecting():
    connector = FakeConnector(rows=[[1]])
    load_snowflake.load("ASSAY_DEMO", "DEMO", days=2, connector=connector)
    assert connector.paramstyle == "qmark"


def test_backfill_only_touches_orders():
    connector = FakeConnector(rows=[[5000]])
    written = load_snowflake.load(
        "ASSAY_DEMO", "DEMO", backfill=True, connector=connector
    )
    assert set(written) == {"orders"}
    assert not any("DROP TABLE" in s for s in _statements(connector))
