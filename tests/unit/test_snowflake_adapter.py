"""Snowflake adapter.

No network: a fake connector stands in for `snowflake.connector`, so the
things that actually differ from DuckDB — identifier folding, bind style,
the read-only guard, credential handling — are all testable offline.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from assay.engine.adapter import Query
from assay.engine.snowflake_adapter import (
    SnowflakeAdapter,
    SnowflakeConfig,
    SnowflakeDialect,
)

ENV = {
    "SNOWFLAKE_ACCOUNT": "acme-eu",
    "SNOWFLAKE_USER": "assay_reader",
    "SNOWFLAKE_WAREHOUSE": "ANALYTICS_WH",
    "SNOWFLAKE_DATABASE": "PROD",
    "SNOWFLAKE_SCHEMA": "MART",
}


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed: list[tuple[str, list]] = []
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._rows

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, rows):
        self.cursors: list[FakeCursor] = []
        self._rows = rows
        self.closed = False

    def cursor(self):
        cursor = FakeCursor(self._rows)
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.closed = True


class FakeConnector:
    paramstyle = "pyformat"

    def __init__(self, rows=()):
        self.connection = FakeConnection(list(rows))
        self.connect_kwargs: dict = {}

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        return self.connection


def _adapter(rows=(), **overrides) -> tuple[SnowflakeAdapter, FakeConnector]:
    connector = FakeConnector(rows)
    adapter = SnowflakeAdapter(
        config=SnowflakeConfig.from_env(ENV), connector=connector, **overrides
    )
    return adapter, connector


# ---- dialect ------------------------------------------------------------


def test_identifiers_fold_to_upper_by_default():
    """`orders` unquoted becomes ORDERS in Snowflake; quoting it lower-case
    would resolve to a different object."""
    assert SnowflakeDialect().quote("orders") == '"ORDERS"'


def test_exact_policy_preserves_case_for_quoted_lowercase_projects():
    assert SnowflakeDialect("exact").quote("orders") == '"orders"'


def test_embedded_quotes_are_escaped():
    assert SnowflakeDialect().quote('we"ird') == '"WE""IRD"'


def test_an_unknown_case_policy_is_rejected():
    with pytest.raises(ValueError, match="unknown case policy"):
        SnowflakeDialect("titlecase")


def test_date_trunc_uses_snowflake_syntax():
    assert SnowflakeDialect().date_trunc("month", "b.TS") == "DATE_TRUNC('MONTH', b.TS)"


# ---- configuration ------------------------------------------------------


def test_config_is_assembled_from_the_environment():
    config = SnowflakeConfig.from_env(ENV)
    assert config.account == "acme-eu"
    assert config.authenticator == "externalbrowser"


def test_missing_settings_are_named():
    with pytest.raises(ValueError, match="SNOWFLAKE_WAREHOUSE"):
        SnowflakeConfig.from_env({**ENV, "SNOWFLAKE_WAREHOUSE": ""})


def test_a_password_in_the_environment_is_refused():
    """Assay authenticates with SSO or a key pair; it never handles a password."""
    with pytest.raises(ValueError, match="not supported"):
        SnowflakeConfig.from_env({**ENV, "SNOWFLAKE_PASSWORD": "hunter2"})


def test_a_private_key_switches_off_browser_auth():
    config = SnowflakeConfig.from_env({**ENV, "SNOWFLAKE_PRIVATE_KEY_FILE": "/k.p8"})
    kwargs = config.connect_kwargs()
    assert kwargs["authenticator"] == "snowflake"
    assert kwargs["private_key_file"] == "/k.p8"


def test_role_is_only_sent_when_configured():
    assert "role" not in SnowflakeConfig.from_env(ENV).connect_kwargs()
    with_role = SnowflakeConfig.from_env({**ENV, "SNOWFLAKE_ROLE": "ASSAY_RO"})
    assert with_role.connect_kwargs()["role"] == "ASSAY_RO"


# ---- adapter behaviour --------------------------------------------------


def test_bind_style_is_switched_to_qmark():
    """Assay generates `?` placeholders; the connector defaults to pyformat."""
    _, connector = _adapter()
    assert connector.paramstyle == "qmark"


def test_rows_come_back_as_tuples():
    adapter, _ = _adapter(rows=[["EMEA", 10.0], ["NA", 20.0]])
    assert adapter.fetch(Query("SELECT a, b FROM t")) == [("EMEA", 10.0), ("NA", 20.0)]


def test_parameters_are_bound_not_interpolated():
    adapter, connector = _adapter(rows=[[1]])
    adapter.fetch(Query("SELECT 1 FROM t WHERE ts >= ?", ("2026-01-01",)))
    sql, params = connector.connection.cursors[0].executed[0]
    assert params == ["2026-01-01"]
    assert "2026-01-01" not in sql


def test_a_cursor_is_closed_even_when_a_query_fails():
    adapter, connector = _adapter(rows=[[1]])
    cursor_holder = {}

    def exploding_execute(sql, params=None):
        raise RuntimeError("SQL compilation error")

    original = connector.connection.cursor

    def tracking_cursor():
        cursor = original()
        cursor.execute = exploding_execute
        cursor_holder["c"] = cursor
        return cursor

    connector.connection.cursor = tracking_cursor
    with pytest.raises(RuntimeError):
        adapter.fetch(Query("SELECT 1"))
    assert cursor_holder["c"].closed


def test_a_non_query_statement_is_refused():
    """Snowflake has no read-only connection flag, so the guarantee is enforced
    here as well as by the role the adapter connects with."""
    adapter, _ = _adapter()
    with pytest.raises(PermissionError, match="non-query statement"):
        adapter.fetch(Query("DELETE FROM orders"))


def test_a_cte_is_allowed():
    adapter, _ = _adapter(rows=[[1]])
    assert adapter.fetch(Query("WITH x AS (SELECT 1) SELECT * FROM x")) == [(1,)]


def test_the_clock_is_injectable():
    as_of = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    adapter, _ = _adapter(as_of=as_of)
    assert adapter.now() == as_of


def test_closing_the_adapter_closes_the_connection():
    adapter, connector = _adapter()
    adapter.close()
    assert connector.connection.closed
