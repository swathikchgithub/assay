"""Adapter selection.

One place decides which warehouse a run talks to, so the CLI stays free of
driver detail and a new target is a new branch here plus a new adapter.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from assay.engine.adapter import WarehouseAdapter

TARGETS = ("duckdb", "snowflake")


def open_adapter(
    target: str,
    as_of: datetime,
    database: Optional[str] = None,
    case_policy: str = "upper",
) -> WarehouseAdapter:
    if target == "duckdb":
        return _duckdb(database, as_of)
    if target == "snowflake":
        from assay.engine.snowflake_adapter import SnowflakeAdapter

        return SnowflakeAdapter(as_of=as_of, case_policy=case_policy)
    raise ValueError(f"unknown target {target!r}; expected one of {TARGETS}")


def _duckdb(database: Optional[str], as_of: datetime) -> WarehouseAdapter:
    from assay.engine.duckdb_adapter import DuckDBAdapter

    if not database:
        raise ValueError("the duckdb target needs --database")
    return DuckDBAdapter(database, as_of=as_of, read_only=True)
