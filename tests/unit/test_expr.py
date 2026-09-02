"""The identity evaluator is the one place a contract file becomes executable."""

import pytest

from assay.contracts.expr import ExpressionError, evaluate, referenced_names


def test_evaluates_subtraction():
    assert evaluate("gross - discounts", {"gross": 100.0, "discounts": 12.0}) == 88.0


def test_evaluates_nested_arithmetic():
    assert evaluate("(a + b) / 2", {"a": 3.0, "b": 5.0}) == 4.0


def test_referenced_names_lists_metrics():
    assert referenced_names("gross - discounts") == frozenset({"gross", "discounts"})


def test_rejects_function_calls():
    with pytest.raises(ExpressionError, match="disallowed expression node"):
        evaluate("__import__('os').system('rm -rf /')", {})


def test_rejects_attribute_access():
    with pytest.raises(ExpressionError, match="disallowed expression node"):
        evaluate("a.__class__", {"a": 1.0})


def test_rejects_unbound_metric():
    with pytest.raises(ExpressionError, match="unbound metric"):
        evaluate("revenue - missing", {"revenue": 10.0})


def test_rejects_division_by_zero():
    with pytest.raises(ExpressionError, match="division by zero"):
        evaluate("a / b", {"a": 1.0, "b": 0.0})


def test_rejects_string_literals():
    with pytest.raises(ExpressionError, match="disallowed literal"):
        evaluate("'sql'", {})
