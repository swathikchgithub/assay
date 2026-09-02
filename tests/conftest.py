"""Shared fixtures.

Unit tests never touch a warehouse: `FakeAdapter` answers by matching a
fragment of the generated SQL, so each invariant is exercised in isolation
against known rows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

import pytest

from assay.contracts.models import ContractSet, Metric
from assay.engine.adapter import Query
from assay.engine.duckdb_adapter import DuckDBDialect
from assay.engine.sql import MetricSQL, Window
from assay.invariants.base import CheckContext

NOW = datetime(2026, 9, 1, 9, 0)


class FakeAdapter:
    """Returns canned rows for the first response key found in the SQL."""

    def __init__(self, responses: dict[str, Iterable[tuple[Any, ...]]]) -> None:
        self.dialect = DuckDBDialect()
        self._responses = responses
        self.calls: list[str] = []

    def fetch(self, query: Query) -> list[tuple[Any, ...]]:
        self.calls.append(query.sql)
        for fragment, rows in self._responses.items():
            if fragment in query.sql:
                return [tuple(r) for r in rows]
        raise AssertionError(f"FakeAdapter has no response for: {query.sql}")

    def now(self) -> datetime:
        return NOW


class FakeHistory:
    def __init__(self, previous: dict[str, dict[str, float]] | None = None) -> None:
        self._previous = previous or {}
        self.recorded: dict[str, dict[str, float]] = {}

    def previous(self, metric: str) -> dict[str, float]:
        return dict(self._previous.get(metric, {}))

    def record(
        self, metric: str, series: dict[str, float], observed_at: datetime
    ) -> None:
        self.recorded[metric] = dict(series)


@pytest.fixture
def dialect() -> DuckDBDialect:
    return DuckDBDialect()


@pytest.fixture
def revenue() -> Metric:
    return Metric(
        name="revenue",
        table="orders",
        measure="sum(amount)",
        time_column="ordered_at",
        additivity="additive",
        freshness_sla_hours=24,
        dimensions=({"name": "region", "column": "name", "table": "regions"},),
        joins=(
            {
                "table": "regions",
                "left_key": "region_code",
                "right_key": "code",
                "kind": "inner",
            },
        ),
    )


@pytest.fixture
def revenue_sql(revenue: Metric, dialect: DuckDBDialect) -> MetricSQL:
    return MetricSQL(revenue, dialect)


@pytest.fixture
def contracts(revenue: Metric) -> ContractSet:
    return ContractSet(metrics=(revenue,))


def context(adapter: FakeAdapter, history: FakeHistory | None = None) -> CheckContext:
    return CheckContext(
        adapter=adapter, window=Window(), now=NOW, history=history
    )
