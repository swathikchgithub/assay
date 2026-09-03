"""Resolving plan time to a concrete window. Pure, no warehouse."""

from datetime import date, datetime

import pytest

from assay.contracts.models import Grain
from assay.engine.sql import Window
from assay.nlq.answer import resolve_window
from assay.nlq.plan import AbsoluteTime, Calendar, Query, Refusal, RelativeTime

NOW = datetime(2026, 9, 17, 14, 30)


def _window(**time_kwargs):
    plan = Query(select=("m",), time=RelativeTime(**time_kwargs))
    return resolve_window(plan, NOW)


def test_all_time_is_an_open_window():
    assert resolve_window(Query(select=("m",)), NOW) == Window()


def test_an_absolute_window_is_passed_through():
    plan = Query(
        select=("m",),
        time=AbsoluteTime(start=date(2026, 1, 1), end=date(2026, 4, 1)),
    )
    assert resolve_window(plan, NOW) == Window(date(2026, 1, 1), date(2026, 4, 1))


def test_this_month():
    assert _window(anchor=Grain.MONTH) == Window(date(2026, 9, 1), date(2026, 10, 1))


def test_last_month():
    assert _window(anchor=Grain.MONTH, offset=-1) == Window(
        date(2026, 8, 1), date(2026, 9, 1)
    )


def test_a_month_offset_crosses_the_year_boundary():
    assert _window(anchor=Grain.MONTH, offset=-9) == Window(
        date(2025, 12, 1), date(2026, 1, 1)
    )


def test_last_quarter():
    """September is Q3, so the previous quarter is April to July."""
    assert _window(anchor=Grain.QUARTER, offset=-1, calendar=Calendar.GREGORIAN) == Window(
        date(2026, 4, 1), date(2026, 7, 1)
    )


def test_last_year():
    assert _window(anchor=Grain.YEAR, offset=-1, calendar=Calendar.GREGORIAN) == Window(
        date(2025, 1, 1), date(2026, 1, 1)
    )


def test_a_week_starts_on_monday():
    """17 September 2026 is a Thursday."""
    assert _window(anchor=Grain.WEEK) == Window(date(2026, 9, 14), date(2026, 9, 21))


def test_yesterday():
    assert _window(anchor=Grain.DAY, offset=-1) == Window(
        date(2026, 9, 16), date(2026, 9, 17)
    )


def test_the_window_is_half_open():
    """End is exclusive, so adjacent periods never double-count a boundary row."""
    august = _window(anchor=Grain.MONTH, offset=-1)
    september = _window(anchor=Grain.MONTH)
    assert august.end == september.start


def test_a_fiscal_period_is_refused_rather_than_guessed():
    result = _window(anchor=Grain.QUARTER, offset=-1, calendar=Calendar.FISCAL)
    assert isinstance(result, Refusal)
    assert result.rule == "NLQ-07"
