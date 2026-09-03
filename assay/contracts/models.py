"""Contract objects — the vocabulary every invariant is generated from.

Contracts are trusted-but-verified: they live in version control and are
reviewed like code, but SQL fragments are still pattern-checked here so a
compromised or careless contract cannot smuggle statements into a query.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FRAGMENT_BANNED = re.compile(r"(;|--|/\*|\*/|\bunion\b|\bdrop\b|\binsert\b)", re.I)


def _check_identifier(value: str) -> str:
    if not IDENTIFIER.match(value):
        raise ValueError(f"not a bare SQL identifier: {value!r}")
    return value


def _check_fragment(value: str) -> str:
    if _FRAGMENT_BANNED.search(value):
        raise ValueError(f"SQL fragment contains a banned token: {value!r}")
    if value.count("(") != value.count(")"):
        raise ValueError(f"unbalanced parentheses in fragment: {value!r}")
    return value


class Additivity(str, Enum):
    """How a metric may be rolled up. See spec section 3.2."""

    ADDITIVE = "additive"
    SEMI_ADDITIVE = "semi_additive"
    NON_ADDITIVE = "non_additive"
    DERIVED = "derived"


class Grain(str, Enum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class JoinType(str, Enum):
    INNER = "inner"
    LEFT = "left"


class Join(BaseModel):
    """A traversal from the base table, as the existing model actually writes it.

    `kind` is recorded rather than corrected: P0 tests the definition a team
    already ships, and an inner join to an incomplete dimension table is one
    of the defects worth surfacing (CON-01), not one to silently repair.

    `required` marks a join the measure or filter itself depends on, so it is
    present even in the ungrouped reference total.
    """

    model_config = ConfigDict(frozen=True)

    table: str
    left_key: str
    right_key: str
    kind: JoinType = JoinType.LEFT
    required: bool = False

    _v = field_validator("table", "left_key", "right_key")(_check_identifier)


class Dimension(BaseModel):
    """A way to slice a metric.

    `domain` is optional and enables STR-05: a plan filtering on a value the
    dimension cannot hold is rejected before the warehouse is touched, with the
    nearest legal value offered. Undeclared means the rule is skipped rather
    than guessed at - `region = 'Northeast'` against a column holding `'NE'`
    returns zero rows silently, and only a declared domain can catch that.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    column: str
    table: Optional[str] = None  # None -> the metric's base table
    domain: Optional[tuple[str, ...]] = None
    synonyms: tuple[str, ...] = ()

    @field_validator("name", "column", "table")
    @classmethod
    def _ident(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _check_identifier(v)


class Metric(BaseModel):
    """An executable, testable metric definition."""

    model_config = ConfigDict(frozen=True)

    name: str
    table: str
    measure: str  # aggregate expression, e.g. "sum(amount)"
    time_column: str
    additivity: Additivity
    dimensions: tuple[Dimension, ...] = ()
    joins: tuple[Join, ...] = ()
    where: Optional[str] = None
    min_grain: Grain = Grain.DAY
    unit: str = "count"
    tolerance: float = Field(default=0.001, ge=0.0, description="relative")
    freshness_sla_hours: Optional[int] = Field(default=None, gt=0)
    derived: Optional[str] = None  # expression over other metrics -> IDN-02
    owner: Optional[str] = None

    @field_validator("name", "table", "time_column")
    @classmethod
    def _ident(cls, v: str) -> str:
        return _check_identifier(v)

    @field_validator("measure", "where")
    @classmethod
    def _fragment(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _check_fragment(v)

    def dimension(self, name: str) -> Dimension:
        for dim in self.dimensions:
            if dim.name == name:
                return dim
        raise KeyError(f"{self.name} has no dimension {name!r}")


class Identity(BaseModel):
    """A declared algebraic relationship between metrics (IDN-01)."""

    model_config = ConfigDict(frozen=True)

    name: str
    lhs: str  # a metric name
    rhs: str  # expression over metric names, evaluated by expr.py
    tolerance: float = Field(default=0.001, ge=0.0)

    @field_validator("name", "lhs")
    @classmethod
    def _ident(cls, v: str) -> str:
        return _check_identifier(v)


class ContractSet(BaseModel):
    """The full vocabulary. Immutable once loaded."""

    model_config = ConfigDict(frozen=True)

    metrics: tuple[Metric, ...]
    identities: tuple[Identity, ...] = ()
    version: str = "unversioned"

    @field_validator("metrics")
    @classmethod
    def _unique_names(cls, v: tuple[Metric, ...]) -> tuple[Metric, ...]:
        names = [m.name for m in v]
        if len(names) != len(set(names)):
            raise ValueError("duplicate metric names in contract set")
        return v

    def metric(self, name: str) -> Metric:
        for m in self.metrics:
            if m.name == name:
                return m
        raise KeyError(f"no metric named {name!r}")

    def has(self, name: str) -> bool:
        return any(m.name == name for m in self.metrics)
