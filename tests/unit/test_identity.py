"""Class III invariants."""

from assay.contracts.models import Identity, Metric
from assay.engine.duckdb_adapter import DuckDBDialect
from assay.engine.sql import MetricSQL
from assay.invariants.base import Status
from assay.invariants.identity import (
    CrossGrainConsistency,
    DeclaredIdentity,
    rollup_is_defined,
)
from tests.conftest import FakeAdapter, context


def _metric(name: str, measure: str, additivity: str = "additive") -> Metric:
    return Metric(
        name=name,
        table=name,
        measure=measure,
        time_column="ts",
        additivity=additivity,
    )


def _sql_map(*metrics: Metric) -> dict[str, MetricSQL]:
    return {m.name: MetricSQL(m, DuckDBDialect()) for m in metrics}


def test_declared_identity_passes_when_it_holds():
    metrics = (_metric("net", "sum(a)"), _metric("gross", "sum(b)"), _metric("disc", "sum(c)"))
    adapter = FakeAdapter(
        {"sum(a)": [(88.0,)], "sum(b)": [(100.0,)], "sum(c)": [(12.0,)]}
    )
    identity = Identity(name="net_def", lhs="net", rhs="gross - disc")
    assert DeclaredIdentity(identity, _sql_map(*metrics)).run(
        context(adapter)
    ).status is Status.PASS


def test_declared_identity_fails_when_a_component_is_double_counted():
    metrics = (_metric("net", "sum(a)"), _metric("gross", "sum(b)"), _metric("disc", "sum(c)"))
    adapter = FakeAdapter(
        {"sum(a)": [(88.0,)], "sum(b)": [(100.0,)], "sum(c)": [(24.0,)]}
    )
    identity = Identity(name="net_def", lhs="net", rhs="gross - disc")
    result = DeclaredIdentity(identity, _sql_map(*metrics)).run(context(adapter))
    assert result.status is Status.FAIL
    assert result.expected == 76.0


def test_declared_identity_skips_rather_than_fails_on_a_bad_expression():
    metric = _metric("net", "sum(a)")
    identity = Identity(name="broken", lhs="net", rhs="net - absent")
    adapter = FakeAdapter({"sum(a)": [(1.0,)]})
    result = DeclaredIdentity(identity, _sql_map(metric)).run(context(adapter))
    assert result.status is Status.SKIP


def test_cross_grain_passes_for_a_genuinely_additive_metric():
    metric = _metric("revenue", "sum(amount)")
    adapter = FakeAdapter(
        {
            "'day'": [("2026-01-01", 5.0), ("2026-01-02", 5.0), ("2026-02-01", 3.0)],
            "'month'": [("2026-01-01", 10.0), ("2026-02-01", 3.0)],
        }
    )
    result = CrossGrainConsistency(metric, MetricSQL(metric, DuckDBDialect())).run(
        context(adapter)
    )
    assert result.status is Status.PASS


def test_cross_grain_catches_a_distinct_count_declared_additive():
    metric = _metric("active_users", "count(distinct user_id)")
    adapter = FakeAdapter(
        {
            "'day'": [("2026-01-01", 400.0), ("2026-01-02", 400.0)],
            "'month'": [("2026-01-01", 500.0)],
        }
    )
    result = CrossGrainConsistency(metric, MetricSQL(metric, DuckDBDialect())).run(
        context(adapter)
    )
    assert result.status is Status.FAIL
    assert "does not roll up that way" in result.detail


def test_semi_additive_rolls_up_by_taking_the_last_value():
    metric = _metric("seats", "sum(seats)", additivity="semi_additive")
    adapter = FakeAdapter(
        {
            "'day'": [("2026-01-01", 90.0), ("2026-01-02", 100.0)],
            "'month'": [("2026-01-01", 100.0)],
        }
    )
    result = CrossGrainConsistency(metric, MetricSQL(metric, DuckDBDialect())).run(
        context(adapter)
    )
    assert result.status is Status.PASS


def test_rollup_is_undefined_for_a_non_additive_metric():
    assert not rollup_is_defined(_metric("rate", "avg(x)", additivity="non_additive"))
