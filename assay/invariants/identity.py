"""Class III — Algebraic identity. Relationships that must hold at any grain.

IDN-03 is the highest-yield check in this class: a metric computed at day and
rolled up must equal the same metric computed natively at month. Anything
grain-dependent — a distinct count mislabelled as additive, a deduplication
that only works inside a day — surfaces here and nowhere else.
"""

from __future__ import annotations

from typing import Mapping

from assay.contracts.expr import ExpressionError, evaluate, referenced_names
from assay.contracts.models import Additivity, Grain, Identity, Metric
from assay.engine.adapter import scalar
from assay.engine.sql import MetricSQL
from assay.invariants.base import (
    CheckContext,
    CheckResult,
    Severity,
    Status,
    relative_delta,
    verdict,
)

_ROLLUP = {
    Additivity.ADDITIVE: sum,
    Additivity.SEMI_ADDITIVE: lambda values: values[-1],
}


class DeclaredIdentity:
    """IDN-01 — a relationship the contract asserts, e.g. gross - discounts = net."""

    severity = Severity.BLOCK

    def __init__(
        self, identity: Identity, sql_by_metric: Mapping[str, MetricSQL]
    ) -> None:
        self._identity = identity
        self._sql = sql_by_metric
        self.id = "IDN-01"
        self.subject = identity.name

    def run(self, ctx: CheckContext) -> CheckResult:
        try:
            values = self._values(ctx)
            expected = evaluate(self._identity.rhs, values)
        except (ExpressionError, KeyError) as exc:
            return CheckResult(self.id, self.subject, Status.SKIP, str(exc))
        observed = values[self._identity.lhs]
        delta = relative_delta(observed, expected)
        violated = delta > self._identity.tolerance
        return CheckResult(
            self.id,
            self.subject,
            verdict(violated, self.severity),
            f"{self._identity.lhs} = {self._identity.rhs}: "
            f"{observed:,.2f} vs {expected:,.2f} ({delta:.2%} apart)",
            observed=observed,
            expected=expected,
            delta=delta,
        )

    def _values(self, ctx: CheckContext) -> dict[str, float]:
        names = referenced_names(self._identity.rhs) | {self._identity.lhs}
        return {
            name: scalar(ctx.fetch(self._sql[name].total(ctx.window)))
            for name in sorted(names)
        }


class DerivedIdentity(DeclaredIdentity):
    """IDN-02 — the same machinery, auto-extracted from `metric.derived`.

    Reported separately because provenance matters when triaging: a declared
    identity failing means two humans disagree, a derived one failing means a
    component changed meaning under a metric that still looks healthy.
    """

    severity = Severity.WARN

    def __init__(self, metric: Metric, sql_by_metric: Mapping[str, MetricSQL]) -> None:
        assert metric.derived is not None
        super().__init__(
            Identity(
                name=metric.name,
                lhs=metric.name,
                rhs=metric.derived,
                tolerance=metric.tolerance,
            ),
            sql_by_metric,
        )
        self.id = "IDN-02"


class CrossGrainConsistency:
    """IDN-03 — day rolled up to month must equal month computed natively."""

    severity = Severity.BLOCK

    def __init__(self, metric: Metric, sql: MetricSQL) -> None:
        self._m, self._sql = metric, sql
        self.id = "IDN-03"
        self.subject = metric.name

    def run(self, ctx: CheckContext) -> CheckResult:
        rolled = self._rolled_up(ctx)
        native = {
            _month(period): float(value)
            for period, value in ctx.fetch(self._sql.by_period(Grain.MONTH, ctx.window))
            if value is not None
        }
        shared = sorted(set(rolled) & set(native))
        if not shared:
            return CheckResult(self.id, self.subject, Status.SKIP, "no overlapping months")
        worst = max(shared, key=lambda k: relative_delta(rolled[k], native[k]))
        delta = relative_delta(rolled[worst], native[worst])
        violated = delta > self._m.tolerance
        return CheckResult(
            self.id,
            self.subject,
            verdict(violated, self.severity),
            self._detail(worst, rolled[worst], native[worst], delta, violated),
            observed=rolled[worst],
            expected=native[worst],
            delta=delta,
        )

    def _rolled_up(self, ctx: CheckContext) -> dict[str, float]:
        rollup = _ROLLUP[self._m.additivity]
        buckets: dict[str, list[float]] = {}
        for period, value in ctx.fetch(self._sql.by_period(Grain.DAY, ctx.window)):
            if value is not None:
                buckets.setdefault(_month(period), []).append(float(value))
        return {month: float(rollup(values)) for month, values in buckets.items()}

    def _detail(
        self, month: str, rolled: float, native: float, delta: float, bad: bool
    ) -> str:
        if not bad:
            return f"daily rollup matches native monthly across {month} and earlier"
        return (
            f"{month}: daily values rolled up give {rolled:,.2f}, computed "
            f"natively at month gives {native:,.2f} ({delta:.2%} apart) — "
            f"`{self._m.name}` is declared {self._m.additivity.value} but does "
            f"not roll up that way"
        )


def rollup_is_defined(metric: Metric) -> bool:
    """IDN-03 only applies where a rollup rule exists (spec section 3.2)."""
    return metric.additivity in _ROLLUP


def _month(period: object) -> str:
    return str(period)[:7]
