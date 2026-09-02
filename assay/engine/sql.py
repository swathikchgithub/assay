"""Query construction for a metric contract.

Every statement an invariant runs is built here, so there is exactly one
place where identifiers are quoted and values are bound as parameters.
Identifiers are validated at contract-load time and quoted here; literal
values are never interpolated (A03: injection).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from assay.contracts.models import Dimension, Grain, Join, Metric
from assay.engine.adapter import Dialect, Query

BASE = "b"


@dataclass(frozen=True)
class Window:
    """Half-open time window [start, end)."""

    start: Optional[date] = None
    end: Optional[date] = None

    @property
    def is_open(self) -> bool:
        return self.start is None and self.end is None


class MetricSQL:
    """Builds the statements the invariant classes need for one metric."""

    def __init__(self, metric: Metric, dialect: Dialect) -> None:
        self._m = metric
        self._d = dialect
        self._alias = {j.table: f"j{i}" for i, j in enumerate(metric.joins)}

    # ---- public builders -------------------------------------------------

    def total(self, window: Window) -> Query:
        """Ungrouped metric value, using only joins the measure depends on."""
        where, params = self._where(window)
        return Query(
            f"SELECT {self._m.measure} FROM {self._from(self._required_joins())}{where}",
            params,
        )

    def grouped(self, dim: Dimension, window: Window) -> Query:
        """Metric grouped by one dimension, NULL group preserved (CON-01, CON-02)."""
        where, params = self._where(window)
        joins = self._required_joins() + self._joins_for(dim)
        expr = self._dimension_expr(dim)
        return Query(
            f"SELECT {expr} AS dim_value, {self._m.measure} AS value "
            f"FROM {self._from(joins)}{where} GROUP BY 1",
            params,
        )

    def by_period(self, grain: Grain, window: Window) -> Query:
        """Metric per time period, ascending (IDN-03, TMP-02/03/04)."""
        where, params = self._where(window)
        period = self._d.date_trunc(grain.value, self._column(self._m.time_column))
        return Query(
            f"SELECT {period} AS period, {self._m.measure} AS value "
            f"FROM {self._from(self._required_joins())}{where} "
            f"GROUP BY 1 ORDER BY 1",
            params,
        )

    def row_counts(self, join: Join, window: Window) -> Query:
        """Base row count vs row count after one traversal (CON-04)."""
        where, params = self._where(window)
        base = f"SELECT count(*) FROM {self._from(self._required_joins())}{where}"
        joined = (
            f"SELECT count(*) FROM "
            f"{self._from(self._required_joins() + [join])}{where}"
        )
        return Query(f"SELECT ({base}), ({joined})", params + params)

    def filter_mass(self, window: Window) -> Query:
        """Rows kept by the metric's own filter, and rows before it (CON-03)."""
        time_where, params = self._where(window, include_metric_filter=False)
        kept = f"sum(CASE WHEN ({self._m.where}) THEN 1 ELSE 0 END)"
        return Query(
            f"SELECT {kept}, count(*) "
            f"FROM {self._from(self._required_joins())}{time_where}",
            params,
        )

    def max_time(self) -> Query:
        """Latest timestamp present (TMP-01)."""
        column = self._column(self._m.time_column)
        return Query(f"SELECT max({column}) FROM {self._from([])}")

    # ---- internals -------------------------------------------------------

    def _from(self, joins: list[Join]) -> str:
        clause = f"{self._d.quote(self._m.table)} AS {BASE}"
        for join in joins:
            alias = self._alias[join.table]
            on = (
                f"{self._column(join.left_key)} = "
                f"{alias}.{self._d.quote(join.right_key)}"
            )
            clause += (
                f" {join.kind.value.upper()} JOIN "
                f"{self._d.quote(join.table)} AS {alias} ON {on}"
            )
        return clause

    def _where(
        self, window: Window, include_metric_filter: bool = True
    ) -> tuple[str, tuple[Any, ...]]:
        clauses: list[str] = []
        params: list[Any] = []
        column = self._column(self._m.time_column)
        if window.start is not None:
            clauses.append(f"{column} >= ?")
            params.append(window.start)
        if window.end is not None:
            clauses.append(f"{column} < ?")
            params.append(window.end)
        if include_metric_filter and self._m.where:
            clauses.append(f"({self._m.where})")
        return (" WHERE " + " AND ".join(clauses) if clauses else "", tuple(params))

    def _column(self, name: str) -> str:
        return f"{BASE}.{self._d.quote(name)}"

    def _dimension_expr(self, dim: Dimension) -> str:
        alias = BASE if dim.table is None else self._alias[dim.table]
        return f"{alias}.{self._d.quote(dim.column)}"

    def _required_joins(self) -> list[Join]:
        return [j for j in self._m.joins if j.required]

    def _joins_for(self, dim: Dimension) -> list[Join]:
        if dim.table is None:
            return []
        return [j for j in self._m.joins if j.table == dim.table and not j.required]
