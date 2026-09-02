"""Class II invariants against known rows."""

from assay.invariants.base import Status
from assay.invariants.conservation import (
    DecompositionSum,
    FilterMass,
    NullMass,
    RowConservation,
)
from tests.conftest import FakeAdapter, context


def _adapter(grouped=None, total=None, counts=None, filtered=None) -> FakeAdapter:
    responses = {}
    if grouped is not None:
        responses["dim_value"] = grouped
    if counts is not None:
        responses["count(*) FROM"] = counts
    if filtered is not None:
        responses["CASE WHEN"] = filtered
    if total is not None:
        responses["SELECT sum(amount) FROM"] = total
    return FakeAdapter(responses)


def test_decomposition_passes_when_parts_sum_to_the_whole(revenue, revenue_sql):
    adapter = _adapter(grouped=[("EMEA", 60.0), ("NA", 40.0)], total=[(100.0,)])
    result = DecompositionSum(revenue, revenue.dimension("region"), revenue_sql).run(
        context(adapter)
    )
    assert result.status is Status.PASS


def test_decomposition_fails_when_a_traversal_drops_rows(revenue, revenue_sql):
    adapter = _adapter(grouped=[("EMEA", 60.0), ("NA", 30.0)], total=[(100.0,)])
    result = DecompositionSum(revenue, revenue.dimension("region"), revenue_sql).run(
        context(adapter)
    )
    assert result.status is Status.FAIL
    assert "disappears" in result.detail


def test_decomposition_names_inflation_rather_than_loss(revenue, revenue_sql):
    adapter = _adapter(grouped=[("EMEA", 150.0)], total=[(100.0,)])
    result = DecompositionSum(revenue, revenue.dimension("region"), revenue_sql).run(
        context(adapter)
    )
    assert "inflates the metric by 50.00%" in result.detail


def test_null_mass_warns_above_the_threshold(revenue, revenue_sql):
    adapter = _adapter(grouped=[("EMEA", 90.0), (None, 10.0)])
    result = NullMass(revenue, revenue.dimension("region"), revenue_sql, 0.01).run(
        context(adapter)
    )
    assert result.status is Status.WARN
    assert result.observed == 0.1


def test_null_mass_passes_when_everything_is_attributed(revenue, revenue_sql):
    adapter = _adapter(grouped=[("EMEA", 90.0), ("NA", 10.0)])
    result = NullMass(revenue, revenue.dimension("region"), revenue_sql, 0.01).run(
        context(adapter)
    )
    assert result.status is Status.PASS


def test_row_conservation_fails_on_fan_out(revenue, revenue_sql):
    adapter = _adapter(counts=[(1000.0, 2500.0)])
    result = RowConservation(revenue, revenue.joins[0], revenue_sql).run(
        context(adapter)
    )
    assert result.status is Status.FAIL
    assert "2.50x" in result.detail


def test_row_conservation_ignores_a_traversal_that_drops_rows(revenue, revenue_sql):
    """A lossy join is CON-01's finding; CON-04 is only about multiplication."""
    adapter = _adapter(counts=[(1000.0, 900.0)])
    result = RowConservation(revenue, revenue.joins[0], revenue_sql).run(
        context(adapter)
    )
    assert result.status is Status.PASS


def test_filter_mass_reports_the_share_removed(revenue, revenue_sql):
    filtered_metric = revenue.model_copy(update={"where": "status <> 'cancelled'"})
    adapter = _adapter(filtered=[(400.0, 1000.0)])
    result = FilterMass(filtered_metric, revenue_sql, 0.5).run(context(adapter))
    assert result.status is Status.WARN
    assert result.observed == 0.6
