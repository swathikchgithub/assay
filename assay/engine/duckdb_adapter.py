"""DuckDB adapter — the P0 execution target and the test substrate."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import duckdb

from assay.engine.adapter import Query


class DuckDBDialect:
    def quote(self, identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def date_trunc(self, grain: str, expression: str) -> str:
        return f"date_trunc('{grain}', {expression})"


class DuckDBAdapter:
    """Read-only DuckDB connection.

    `as_of` injects the clock so temporal checks (TMP-01, TMP-04) are
    deterministic in tests rather than depending on wall time.
    """

    def __init__(
        self,
        database: str = ":memory:",
        as_of: Optional[datetime] = None,
        read_only: bool = False,
    ) -> None:
        self.dialect = DuckDBDialect()
        self._as_of = as_of
        if database != ":memory:" and read_only and not Path(database).exists():
            raise FileNotFoundError(database)
        self._conn = duckdb.connect(database, read_only=read_only)

    def fetch(self, query: Query) -> list[tuple[Any, ...]]:
        return self._conn.execute(query.sql, list(query.params)).fetchall()

    def execute(self, sql: str) -> None:
        """Set-up only — used by the demo seeder, never by an invariant."""
        self._conn.execute(sql)

    def now(self) -> datetime:
        return self._as_of or datetime.now(timezone.utc)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "DuckDBAdapter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
