"""Contract validation is the first line of defence against a bad definition."""

import pytest
from pydantic import ValidationError

from assay.contracts.models import Additivity, ContractSet, Metric


def _metric(**overrides) -> Metric:
    base = {
        "name": "revenue",
        "table": "orders",
        "measure": "sum(amount)",
        "time_column": "ordered_at",
        "additivity": "additive",
    }
    return Metric(**{**base, **overrides})


def test_accepts_a_well_formed_metric():
    assert _metric().additivity is Additivity.ADDITIVE


def test_rejects_a_non_identifier_table():
    with pytest.raises(ValidationError, match="not a bare SQL identifier"):
        _metric(table="orders; DROP TABLE users")


def test_rejects_a_statement_terminator_in_a_measure():
    with pytest.raises(ValidationError, match="banned token"):
        _metric(measure="sum(amount); DELETE FROM orders")


def test_rejects_unbalanced_parentheses_in_a_filter():
    with pytest.raises(ValidationError, match="unbalanced parentheses"):
        _metric(where="(status = 'ok'")


def test_rejects_duplicate_metric_names():
    with pytest.raises(ValidationError, match="duplicate metric names"):
        ContractSet(metrics=(_metric(), _metric()))


def test_rejects_a_negative_tolerance():
    with pytest.raises(ValidationError):
        _metric(tolerance=-0.1)


def test_metric_lookup_raises_for_unknown_name():
    with pytest.raises(KeyError):
        ContractSet(metrics=(_metric(),)).metric("nope")
