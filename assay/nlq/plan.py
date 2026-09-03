"""The plan IR — a closed grammar over metrics and dimensions.

The one design constraint everything else rests on: **a plan can name metrics
and dimensions, and nothing else.** No table names, no join clauses, no SQL
fragments. Whole classes of error — fan-out, wrong join key, silent grain
mismatch — become unrepresentable rather than merely detectable.

v1 covers `Query`. `Compare`, `Trend`, `Rank` and `Decompose` from the
architecture spec arrive with the operations that need them.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from assay.contracts.models import Grain


class Op(str, Enum):
    EQ = "eq"
    NEQ = "neq"
    IN = "in"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"


class Filter(BaseModel):
    model_config = ConfigDict(frozen=True)

    dimension: str
    op: Op
    value: Any

    @property
    def values(self) -> tuple[Any, ...]:
        """Filter values as a tuple, whether one was given or many."""
        if isinstance(self.value, (list, tuple)):
            return tuple(self.value)
        return (self.value,)


class Calendar(str, Enum):
    """Which calendar a relative period is anchored to.

    Not a detail. "Last quarter" means different things to Finance and to
    Sales, and a silent Gregorian default is a reliable way to lose an
    executive permanently — so an unresolved calendar is a rejection, not a
    default.
    """

    GREGORIAN = "gregorian"
    FISCAL = "fiscal"


class AllTime(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["all"] = "all"


class AbsoluteTime(BaseModel):
    """Half-open interval [start, end)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["absolute"] = "absolute"
    start: date
    end: date

    @field_validator("end")
    @classmethod
    def _ordered(cls, end: date, info: Any) -> date:
        start = info.data.get("start")
        if start is not None and end <= start:
            raise ValueError("end must be after start (the interval is half-open)")
        return end


class RelativeTime(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["relative"] = "relative"
    anchor: Grain
    offset: int = 0  # 0 = current period, -1 = the one before
    calendar: Optional[Calendar] = None


TimeSpec = Union[AllTime, AbsoluteTime, RelativeTime]


class Query(BaseModel):
    """One metric question. The only plan shape in v1."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["query"] = "query"
    select: tuple[str, ...] = Field(min_length=1)
    by: tuple[str, ...] = ()
    where: tuple[Filter, ...] = ()
    time: TimeSpec = AllTime()
    grain: Optional[Grain] = None  # set for a time series rather than a total

    @field_validator("select", "by")
    @classmethod
    def _no_duplicates(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(names)) != len(names):
            raise ValueError("duplicate names")
        return names

    @property
    def dimensions_used(self) -> tuple[str, ...]:
        """Every dimension the plan touches, grouped or filtered."""
        return tuple(dict.fromkeys(self.by + tuple(f.dimension for f in self.where)))


class Refusal(BaseModel):
    """Why a plan was not run, in language the asker can act on.

    `concept` is what the plan wanted and could not have. Grouping refusals by
    it produces a demand-ranked list of the semantics a business needs next,
    written by users in their own words.
    """

    model_config = ConfigDict(frozen=True)

    rule: str
    reason: str
    repair: Optional[str] = None
    concept: Optional[str] = None
