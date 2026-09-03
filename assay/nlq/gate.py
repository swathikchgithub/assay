"""The type gate — deterministic validation of a plan against the contracts.

This is the component that makes a language model safe to put in front of a
warehouse. It is pure functions over the plan and the contract set: no
warehouse access, no model judgment, sub-millisecond.

The spike measured what it is worth. A model with 75% refusal accuracy
produced two plans that would have returned confidently wrong numbers —
slicing metrics that declare no dimensions — and the gate rejected both. The
model buys answer quality; the gate buys correctness.

Rules enforced here are STR-01, 02, 03, 05 and 08. STR-04 (rollup legality),
STR-06 (unique join path) and STR-07 (fan-out safety) need the compiler's path
resolution and arrive with it; claiming them now would be a rule that never
fires.
"""

from __future__ import annotations

from difflib import get_close_matches
from typing import Optional

from assay.contracts.models import ContractSet, Dimension, Grain, Metric
from assay.nlq.plan import Calendar, Filter, Query, Refusal

# Week and month are not strictly nested, but a floor comparison only needs a
# consistent ordering and this is the conventional one.
_GRAIN_ORDER = {
    Grain.DAY: 0,
    Grain.WEEK: 1,
    Grain.MONTH: 2,
    Grain.QUARTER: 3,
    Grain.YEAR: 4,
}

_CALENDAR_SENSITIVE = (Grain.QUARTER, Grain.YEAR)


def check(plan: Query, contracts: ContractSet) -> tuple[Refusal, ...]:
    """Every reason this plan must not run. Empty means it may.

    Time: O(metrics x dimensions). Space: O(refusals). No I/O.
    """
    refusals = list(_str01(plan, contracts))
    if refusals:
        return tuple(refusals)  # nothing downstream is meaningful without metrics
    refusals += _str02(plan, contracts)
    refusals += _str03(plan, contracts)
    refusals += _str05(plan, contracts)
    refusals += _str08(plan)
    return tuple(refusals)


def is_legal(plan: Query, contracts: ContractSet) -> bool:
    return not check(plan, contracts)


# ---- STR-01 · the metric exists ----------------------------------------------


def _str01(plan: Query, contracts: ContractSet) -> list[Refusal]:
    known = [m.name for m in contracts.metrics]
    return [
        Refusal(
            rule="STR-01",
            reason=f"no metric named {name!r}",
            repair=_nearest(name, known, "Did you mean {}?"),
            concept=name,
        )
        for name in plan.select
        if not contracts.has(name)
    ]


# ---- STR-02 · the dimension is reachable -------------------------------------


def _str02(plan: Query, contracts: ContractSet) -> list[Refusal]:
    out: list[Refusal] = []
    for name in plan.dimensions_used:
        for metric in _selected(plan, contracts):
            if _dimension(metric, name) is None:
                out.append(_unreachable(metric, name))
    return out


def _unreachable(metric: Metric, name: str) -> Refusal:
    available = [d.name for d in metric.dimensions]
    canonical = _by_synonym(metric, name)
    if canonical:
        repair = f"{metric.name} calls that {canonical!r}."
    elif available:
        repair = f"{metric.name} can be sliced by: {', '.join(sorted(available))}."
    else:
        repair = f"{metric.name} declares no dimensions and cannot be sliced."
    return Refusal(
        rule="STR-02",
        reason=f"{metric.name} cannot be sliced by {name!r}",
        repair=repair,
        concept=f"{metric.name}.{name}",
    )


# ---- STR-03 · the grain floor -------------------------------------------------


def _str03(plan: Query, contracts: ContractSet) -> list[Refusal]:
    if plan.grain is None:
        return []
    return [
        Refusal(
            rule="STR-03",
            reason=(
                f"{m.name} is only defined down to {m.min_grain.value}, "
                f"so it cannot be shown by {plan.grain.value}"
            ),
            repair=f"Ask for {m.name} by {m.min_grain.value} or coarser.",
            concept=f"{m.name}@{plan.grain.value}",
        )
        for m in _selected(plan, contracts)
        if _GRAIN_ORDER[plan.grain] < _GRAIN_ORDER[m.min_grain]
    ]


# ---- STR-05 · filter values exist ---------------------------------------------


def _str05(plan: Query, contracts: ContractSet) -> list[Refusal]:
    """A value the column cannot hold returns zero rows, silently.

    Only checkable where the contract declares a domain; undeclared dimensions
    are skipped rather than guessed at.
    """
    out: list[Refusal] = []
    for clause in plan.where:
        for metric in _selected(plan, contracts):
            dim = _dimension(metric, clause.dimension)
            if dim is None or not dim.domain:
                continue
            out.extend(_bad_values(clause, dim))
    return out


def _bad_values(clause: Filter, dim: Dimension) -> list[Refusal]:
    domain = list(dim.domain or ())
    return [
        Refusal(
            rule="STR-05",
            reason=f"{dim.name} has no value {value!r}",
            repair=_nearest(str(value), domain, "Did you mean {}?")
            or f"{dim.name} holds: {', '.join(domain)}.",
            concept=f"{dim.name}={value}",
        )
        for value in clause.values
        if isinstance(value, str) and value not in domain
    ]


# ---- STR-08 · the calendar is resolved ----------------------------------------


def _str08(plan: Query) -> list[Refusal]:
    time = plan.time
    if getattr(time, "kind", None) != "relative":
        return []
    if time.anchor not in _CALENDAR_SENSITIVE or time.calendar is not None:
        return []
    return [
        Refusal(
            rule="STR-08",
            reason=f"'{time.anchor.value}' is ambiguous without a calendar",
            repair=(
                f"Say which: {Calendar.FISCAL.value} or {Calendar.GREGORIAN.value}. "
                "They start on different days and give different numbers."
            ),
            concept=f"calendar:{time.anchor.value}",
        )
    ]


# ---- shared -------------------------------------------------------------------


def _selected(plan: Query, contracts: ContractSet) -> list[Metric]:
    return [contracts.metric(n) for n in plan.select if contracts.has(n)]


def _dimension(metric: Metric, name: str) -> Optional[Dimension]:
    for dim in metric.dimensions:
        if dim.name == name:
            return dim
    return None


def _by_synonym(metric: Metric, name: str) -> Optional[str]:
    lowered = name.lower()
    for dim in metric.dimensions:
        if lowered in {s.lower() for s in dim.synonyms}:
            return dim.name
    return None


def _nearest(value: str, candidates: list[str], template: str) -> Optional[str]:
    match = get_close_matches(value, candidates, n=1, cutoff=0.6)
    return template.format(repr(match[0])) if match else None
