"""The plan IR. A plan names metrics and dimensions and nothing else."""

import pytest
from pydantic import ValidationError

from assay.contracts.models import Grain
from assay.nlq.plan import AbsoluteTime, AllTime, Calendar, Filter, Query, RelativeTime


def test_a_plan_must_select_something():
    with pytest.raises(ValidationError):
        Query(select=())


def test_duplicate_metrics_are_rejected():
    with pytest.raises(ValidationError, match="duplicate"):
        Query(select=("net_revenue", "net_revenue"))


def test_duplicate_dimensions_are_rejected():
    with pytest.raises(ValidationError, match="duplicate"):
        Query(select=("net_revenue",), by=("region", "region"))


def test_a_plan_defaults_to_all_time():
    assert isinstance(Query(select=("x",)).time, AllTime)


def test_dimensions_used_covers_grouping_and_filtering():
    plan = Query(
        select=("net_revenue",),
        by=("region",),
        where=(Filter(dimension="segment", op="eq", value="Enterprise"),),
    )
    assert plan.dimensions_used == ("region", "segment")


def test_a_dimension_grouped_and_filtered_is_listed_once():
    plan = Query(
        select=("net_revenue",),
        by=("region",),
        where=(Filter(dimension="region", op="eq", value="EMEA"),),
    )
    assert plan.dimensions_used == ("region",)


def test_filter_values_normalise_scalars_and_lists():
    assert Filter(dimension="d", op="eq", value="a").values == ("a",)
    assert Filter(dimension="d", op="in", value=["a", "b"]).values == ("a", "b")


def test_an_absolute_interval_must_be_ordered():
    from datetime import date

    with pytest.raises(ValidationError, match="half-open"):
        AbsoluteTime(start=date(2026, 6, 1), end=date(2026, 6, 1))


def test_relative_time_leaves_the_calendar_unset_by_default():
    """Unset is what STR-08 refuses. Defaulting to Gregorian would hide it."""
    assert RelativeTime(anchor=Grain.QUARTER, offset=-1).calendar is None


def test_a_plan_round_trips_through_json():
    plan = Query(
        select=("net_revenue",),
        by=("region",),
        time=RelativeTime(anchor=Grain.QUARTER, offset=-1, calendar=Calendar.FISCAL),
    )
    assert Query.model_validate_json(plan.model_dump_json()) == plan


def test_a_plan_cannot_name_a_table():
    """The constraint the whole design rests on — there is no field for it."""
    assert "table" not in Query.model_fields
    assert "sql" not in Query.model_fields
    assert "join" not in Query.model_fields
