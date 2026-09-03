"""Executing plans against the real demo warehouse.

The assertion that matters is scoping: a proof card must carry the caveats
that bear on *this* number and not every true statement about the metric.
"""

from datetime import datetime

import pytest

from assay.contracts.sources import YamlSource
from assay.engine.duckdb_adapter import DuckDBAdapter
from assay.nlq.answer import execute
from assay.nlq.plan import Filter, Query, RelativeTime
from assay.contracts.models import Grain
from demo import seed

AS_OF = datetime(2026, 9, 1, 9, 0)


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory):
    path = tmp_path_factory.mktemp("answer") / "w.duckdb"
    seed.seed(path, days=200)
    return path


@pytest.fixture(scope="module")
def contracts():
    return YamlSource("demo/contracts.yml").load()


def _run(warehouse, contracts, plan):
    with DuckDBAdapter(str(warehouse), as_of=AS_OF) as adapter:
        return execute(plan, contracts, adapter)


def _subjects(answer) -> set[str]:
    return {f"{c.invariant_id} {c.subject}" for c in answer.checks}


def test_an_ungrouped_total_comes_back(warehouse, contracts):
    answer = _run(warehouse, contracts, Query(select=("net_revenue",)))
    assert answer.results[0].value > 0
    assert answer.results[0].rows == ()


def test_a_grouped_query_returns_rows_that_sum_to_the_value(warehouse, contracts):
    answer = _run(warehouse, contracts, Query(select=("net_revenue",), by=("region",)))
    result = answer.results[0]
    assert len(result.rows) == 3
    assert result.value == pytest.approx(sum(v for _, v in result.rows))


def test_rows_come_back_largest_first(warehouse, contracts):
    answer = _run(warehouse, contracts, Query(select=("net_revenue",), by=("region",)))
    values = [v for _, v in answer.results[0].rows]
    assert values == sorted(values, reverse=True)


def test_a_filter_reduces_the_value(warehouse, contracts):
    everything = _run(warehouse, contracts, Query(select=("net_revenue",)))
    filtered = _run(
        warehouse,
        contracts,
        Query(
            select=("net_revenue",),
            where=(Filter(dimension="segment", op="eq", value="Enterprise"),),
        ),
    )
    assert 0 < filtered.results[0].value < everything.results[0].value


def test_a_relative_window_narrows_the_result(warehouse, contracts):
    everything = _run(warehouse, contracts, Query(select=("net_revenue",)))
    month = _run(
        warehouse,
        contracts,
        Query(select=("net_revenue",), time=RelativeTime(anchor=Grain.MONTH, offset=-1)),
    )
    assert month.results[0].value < everything.results[0].value


# ---- check scoping ------------------------------------------------------------


def test_a_traversed_path_is_checked(warehouse, contracts):
    answer = _run(warehouse, contracts, Query(select=("net_revenue",), by=("sku",)))
    assert "CON-04 net_revenue -> order_items" in _subjects(answer)


def test_an_untraversed_path_is_not_mentioned(warehouse, contracts):
    """The sku fan-out is real, and irrelevant to a question about regions."""
    answer = _run(warehouse, contracts, Query(select=("net_revenue",), by=("region",)))
    assert "CON-04 net_revenue -> order_items" not in _subjects(answer)


def test_a_filtered_dimension_also_counts_as_traversed(warehouse, contracts):
    answer = _run(
        warehouse,
        contracts,
        Query(
            select=("net_revenue",),
            where=(Filter(dimension="segment", op="eq", value="Enterprise"),),
        ),
    )
    assert "CON-04 net_revenue -> accounts" in _subjects(answer)


def test_an_unsliced_question_carries_only_freshness(warehouse, contracts):
    answer = _run(warehouse, contracts, Query(select=("net_revenue",)))
    assert _subjects(answer) == {"TMP-01 net_revenue"}


def test_a_slice_through_a_broken_path_is_not_trustworthy(warehouse, contracts):
    """Region joins through a lookup table missing a region."""
    answer = _run(warehouse, contracts, Query(select=("net_revenue",), by=("region",)))
    assert not answer.trustworthy


def test_a_clean_question_is_trustworthy(warehouse, contracts):
    answer = _run(warehouse, contracts, Query(select=("net_revenue",)))
    assert answer.trustworthy


def test_scheduled_checks_are_not_run_inline(warehouse, contracts):
    """Restatement and identity need history and full series — too slow to wait on."""
    answer = _run(warehouse, contracts, Query(select=("net_revenue",), by=("region",)))
    assert not any(c.invariant_id.startswith(("TMP-02", "TMP-03", "IDN")) for c in answer.checks)


def test_grouping_by_two_dimensions_is_refused_for_now(warehouse, contracts):
    answer = _run(
        warehouse, contracts, Query(select=("net_revenue",), by=("region", "segment"))
    )
    assert answer.refusals[0].rule == "NLQ-06"
    assert answer.results == ()
