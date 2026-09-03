"""The type gate.

The spike measured what this is worth: a model with 75% refusal accuracy
produced two plans that would have returned confidently wrong numbers, and the
gate rejected both. These tests pin that behaviour.
"""

import pytest

from assay.contracts.models import ContractSet, Grain, Metric
from assay.nlq.gate import check, is_legal
from assay.nlq.plan import AbsoluteTime, Calendar, Filter, Query, RelativeTime


def _metric(**overrides) -> Metric:
    base = {
        "name": "net_revenue",
        "table": "orders",
        "measure": "sum(amount)",
        "time_column": "ordered_at",
        "additivity": "additive",
        "dimensions": (
            {
                "name": "region",
                "column": "region",
                "domain": ("EMEA", "NA", "APAC"),
                "synonyms": ("geo", "territory"),
            },
            {"name": "sku", "column": "sku"},
        ),
    }
    return Metric(**{**base, **overrides})


@pytest.fixture
def contracts() -> ContractSet:
    return ContractSet(
        metrics=(
            _metric(),
            _metric(name="active_users", measure="count(distinct u)", dimensions=()),
            _metric(name="monthly_target", dimensions=(), min_grain="month"),
        )
    )


def _rules(plan, contracts) -> list[str]:
    return [r.rule for r in check(plan, contracts)]


# ---- STR-01 -------------------------------------------------------------------


def test_a_known_metric_passes(contracts):
    assert is_legal(Query(select=("net_revenue",)), contracts)


def test_an_unknown_metric_is_refused(contracts):
    refusals = check(Query(select=("churn_rate",)), contracts)
    assert refusals[0].rule == "STR-01"
    assert "churn_rate" in refusals[0].reason


def test_an_unknown_metric_is_logged_as_a_coverage_gap(contracts):
    """Grouping refusals by concept is the roadmap users write themselves."""
    assert check(Query(select=("churn_rate",)), contracts)[0].concept == "churn_rate"


def test_a_near_miss_metric_gets_a_suggestion(contracts):
    refusals = check(Query(select=("net_revenu",)), contracts)
    assert "net_revenue" in refusals[0].repair


def test_an_unknown_metric_short_circuits_the_rest(contracts):
    """Nothing downstream is meaningful once the metric does not exist."""
    plan = Query(select=("nope",), by=("region",), grain=Grain.DAY)
    assert _rules(plan, contracts) == ["STR-01"]


# ---- STR-02 -------------------------------------------------------------------


def test_a_reachable_dimension_passes(contracts):
    assert is_legal(Query(select=("net_revenue",), by=("region",)), contracts)


def test_slicing_a_metric_that_declares_no_dimensions_is_refused(contracts):
    """The exact error the weaker model made in the spike, twice."""
    refusals = check(Query(select=("active_users",), by=("region",)), contracts)
    assert refusals[0].rule == "STR-02"
    assert "declares no dimensions" in refusals[0].repair


def test_an_unreachable_dimension_lists_what_is_available(contracts):
    refusals = check(Query(select=("net_revenue",), by=("country",)), contracts)
    assert "region" in refusals[0].repair and "sku" in refusals[0].repair


def test_a_synonym_is_answered_with_the_canonical_name(contracts):
    refusals = check(Query(select=("net_revenue",), by=("geo",)), contracts)
    assert refusals[0].repair == "net_revenue calls that 'region'."


def test_a_filtered_dimension_is_checked_too(contracts):
    plan = Query(
        select=("active_users",),
        where=(Filter(dimension="region", op="eq", value="EMEA"),),
    )
    assert _rules(plan, contracts) == ["STR-02"]


def test_every_selected_metric_must_reach_the_dimension(contracts):
    plan = Query(select=("net_revenue", "active_users"), by=("region",))
    assert _rules(plan, contracts) == ["STR-02"]


# ---- STR-03 -------------------------------------------------------------------


def test_a_grain_finer_than_the_metric_allows_is_refused(contracts):
    plan = Query(select=("monthly_target",), grain=Grain.DAY)
    refusals = check(plan, contracts)
    assert refusals[0].rule == "STR-03"
    assert "only defined down to month" in refusals[0].reason


def test_a_coarser_grain_is_allowed(contracts):
    assert is_legal(Query(select=("monthly_target",), grain=Grain.QUARTER), contracts)


def test_the_metrics_own_grain_is_allowed(contracts):
    assert is_legal(Query(select=("monthly_target",), grain=Grain.MONTH), contracts)


def test_no_grain_means_no_grain_check(contracts):
    assert is_legal(Query(select=("monthly_target",)), contracts)


# ---- STR-05 -------------------------------------------------------------------


def test_a_value_outside_the_domain_is_refused(contracts):
    plan = Query(
        select=("net_revenue",),
        where=(Filter(dimension="region", op="eq", value="Northeast"),),
    )
    refusals = check(plan, contracts)
    assert refusals[0].rule == "STR-05"


def test_a_near_miss_value_gets_a_suggestion(contracts):
    plan = Query(
        select=("net_revenue",),
        where=(Filter(dimension="region", op="eq", value="EMEAA"),),
    )
    assert "EMEA" in check(plan, contracts)[0].repair


def test_a_far_miss_value_lists_the_domain(contracts):
    plan = Query(
        select=("net_revenue",),
        where=(Filter(dimension="region", op="eq", value="Mars"),),
    )
    assert "EMEA, NA, APAC" in check(plan, contracts)[0].repair


def test_every_value_of_an_in_clause_is_checked(contracts):
    plan = Query(
        select=("net_revenue",),
        where=(Filter(dimension="region", op="in", value=["EMEA", "Atlantis"]),),
    )
    refusals = check(plan, contracts)
    assert len(refusals) == 1 and "Atlantis" in refusals[0].reason


def test_a_dimension_with_no_declared_domain_is_skipped(contracts):
    """Undeclared means unknown. Guessing would invent a rule."""
    plan = Query(
        select=("net_revenue",),
        where=(Filter(dimension="sku", op="eq", value="SKU-999"),),
    )
    assert is_legal(plan, contracts)


# ---- STR-08 -------------------------------------------------------------------


def test_a_relative_quarter_without_a_calendar_is_refused(contracts):
    plan = Query(select=("net_revenue",), time=RelativeTime(anchor=Grain.QUARTER))
    refusals = check(plan, contracts)
    assert refusals[0].rule == "STR-08"
    assert "fiscal" in refusals[0].repair


def test_a_relative_quarter_with_a_calendar_passes(contracts):
    plan = Query(
        select=("net_revenue",),
        time=RelativeTime(anchor=Grain.QUARTER, calendar=Calendar.FISCAL),
    )
    assert is_legal(plan, contracts)


def test_a_relative_month_needs_no_calendar(contracts):
    """Months start on the first in every calendar anyone uses."""
    plan = Query(select=("net_revenue",), time=RelativeTime(anchor=Grain.MONTH))
    assert is_legal(plan, contracts)


def test_an_absolute_window_is_never_ambiguous(contracts):
    from datetime import date

    plan = Query(
        select=("net_revenue",),
        time=AbsoluteTime(start=date(2026, 1, 1), end=date(2026, 4, 1)),
    )
    assert is_legal(plan, contracts)


# ---- accumulation -------------------------------------------------------------


def test_independent_problems_are_all_reported(contracts):
    plan = Query(
        select=("net_revenue",),
        by=("country",),
        where=(Filter(dimension="region", op="eq", value="Mars"),),
        time=RelativeTime(anchor=Grain.YEAR),
    )
    assert set(_rules(plan, contracts)) == {"STR-02", "STR-05", "STR-08"}


def test_the_gate_touches_no_warehouse(contracts):
    """It is a pure function of plan and contracts — no adapter is involved."""
    import inspect

    from assay.nlq import gate

    assert "adapter" not in inspect.signature(gate.check).parameters
    assert "Adapter" not in inspect.getsource(gate)
