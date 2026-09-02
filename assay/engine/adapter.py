"""Warehouse seam.

Invariants never touch a driver. They ask a `WarehouseAdapter` for rows, so
the same check suite runs against DuckDB in tests and Snowflake in production
without a line changing (Dependency Inversion).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, Sequence


@dataclass(frozen=True)
class Query:
    """A compiled statement plus its bound parameters."""

    sql: str
    params: tuple[Any, ...] = ()


class Dialect(Protocol):
    """The handful of things that differ between warehouses."""

    def quote(self, identifier: str) -> str: ...

    def date_trunc(self, grain: str, expression: str) -> str: ...


class WarehouseAdapter(Protocol):
    """Read-only access to the warehouse.

    Deliberately read-only: Assay observes, and a verification layer that can
    write is a verification layer that can corrupt what it verifies.
    """

    dialect: Dialect

    def fetch(self, query: Query) -> list[tuple[Any, ...]]: ...

    def now(self) -> datetime: ...


def scalar(rows: Sequence[tuple[Any, ...]], default: float = 0.0) -> float:
    """First column of the first row as a float, or `default` when empty/NULL."""
    if not rows or rows[0][0] is None:
        return default
    return float(rows[0][0])
