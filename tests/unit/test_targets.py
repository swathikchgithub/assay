"""Target selection — the only place the CLI learns about drivers."""

from datetime import datetime, timezone

import pytest

from assay.engine.targets import open_adapter

AS_OF = datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_duckdb_requires_a_database_path():
    with pytest.raises(ValueError, match="needs --database"):
        open_adapter("duckdb", AS_OF)


def test_an_unknown_target_names_the_valid_ones():
    with pytest.raises(ValueError, match="expected one of"):
        open_adapter("redshift", AS_OF, "x.duckdb")


def test_duckdb_opens_read_only(tmp_path):
    import duckdb

    path = tmp_path / "w.duckdb"
    duckdb.connect(str(path)).close()
    adapter = open_adapter("duckdb", AS_OF, str(path))
    assert adapter.now() == AS_OF
    adapter.close()


def test_snowflake_is_not_imported_unless_selected(monkeypatch, tmp_path):
    """The connector is a heavy optional dependency; selecting duckdb must not
    pay for it."""
    import sys

    import duckdb

    path = tmp_path / "w.duckdb"
    duckdb.connect(str(path)).close()
    monkeypatch.delitem(sys.modules, "snowflake.connector", raising=False)
    open_adapter("duckdb", AS_OF, str(path)).close()
    assert "snowflake.connector" not in sys.modules
