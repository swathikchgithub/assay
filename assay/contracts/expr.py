"""Restricted arithmetic evaluator for identity right-hand sides.

Identity expressions come from contract files and are evaluated against
metric values. `eval()` is never used: the expression is parsed to an AST
and every node type is checked against an allow-list, so a contract cannot
execute code even if the file is compromised.

Time:  O(n) in AST nodes.  Space: O(depth).
"""

from __future__ import annotations

import ast
import operator
from typing import Callable, Mapping

_BINARY: Mapping[type, Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}

_UNARY: Mapping[type, Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


class ExpressionError(ValueError):
    """Raised for a malformed, unsafe, or unresolvable expression."""


def referenced_names(expression: str) -> frozenset[str]:
    """Metric names an expression depends on."""
    tree = _parse(expression)
    return frozenset(
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    )


def evaluate(expression: str, values: Mapping[str, float]) -> float:
    """Evaluate `expression` with metric names bound to `values`."""
    return _eval_node(_parse(expression), values)


def _parse(expression: str) -> ast.expr:
    try:
        return ast.parse(expression, mode="eval").body
    except SyntaxError as exc:
        raise ExpressionError(f"cannot parse {expression!r}: {exc}") from exc


def _eval_node(node: ast.expr, values: Mapping[str, float]) -> float:
    if isinstance(node, ast.Constant):
        return _constant(node)
    if isinstance(node, ast.Name):
        return _name(node, values)
    if isinstance(node, ast.BinOp):
        return _binop(node, values)
    if isinstance(node, ast.UnaryOp):
        return _unaryop(node, values)
    raise ExpressionError(f"disallowed expression node: {type(node).__name__}")


def _constant(node: ast.Constant) -> float:
    if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
        raise ExpressionError(f"disallowed literal: {node.value!r}")
    return float(node.value)


def _name(node: ast.Name, values: Mapping[str, float]) -> float:
    if node.id not in values:
        raise ExpressionError(f"unbound metric in expression: {node.id!r}")
    return float(values[node.id])


def _binop(node: ast.BinOp, values: Mapping[str, float]) -> float:
    op = _BINARY.get(type(node.op))
    if op is None:
        raise ExpressionError(f"disallowed operator: {type(node.op).__name__}")
    right = _eval_node(node.right, values)
    if isinstance(node.op, ast.Div) and right == 0:
        raise ExpressionError("division by zero in identity expression")
    return op(_eval_node(node.left, values), right)


def _unaryop(node: ast.UnaryOp, values: Mapping[str, float]) -> float:
    op = _UNARY.get(type(node.op))
    if op is None:
        raise ExpressionError(f"disallowed unary operator: {type(node.op).__name__}")
    return op(_eval_node(node.operand, values))
