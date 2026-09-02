"""Class II — Conservation. Assertions that the arithmetic of a result is closed.

These catch the largest share of real defects: a partition silently dropped by
an inner join, a slice that omits unattributed rows, a filter far more
aggressive than anyone intended, a traversal that multiplies the base table.
"""

from __future__ import annotations

from assay.contracts.models import Dimension, Join, Metric
from assay.engine.adapter import scalar
from assay.engine.sql import MetricSQL
from assay.invariants.base import (
    CheckContext,
    CheckResult,
    Severity,
    relative_delta,
    verdict,
)


class DecompositionSum:
    """CON-01 — the parts must sum to the whole.

    Compares the metric grouped by one dimension against the ungrouped total.
    The two statements differ by the traversal needed to reach the dimension,
    so a lossy join shows up here as missing value rather than as nothing.
    """

    severity = Severity.BLOCK

    def __init__(self, metric: Metric, dimension: Dimension, sql: MetricSQL) -> None:
        self._m, self._dim, self._sql = metric, dimension, sql
        self.id = "CON-01"
        self.subject = f"{metric.name} by {dimension.name}"

    def run(self, ctx: CheckContext) -> CheckResult:
        rows = ctx.fetch(self._sql.grouped(self._dim, ctx.window))
        grouped = sum(float(v) for _, v in rows if v is not None)
        total = scalar(ctx.fetch(self._sql.total(ctx.window)))
        delta = relative_delta(grouped, total)
        violated = delta > self._m.tolerance
        return CheckResult(
            self.id,
            self.subject,
            verdict(violated, self.severity),
            self._detail(grouped, total, delta, violated),
            observed=grouped,
            expected=total,
            delta=delta,
        )

    def _detail(self, grouped: float, total: float, delta: float, bad: bool) -> str:
        if not bad:
            return f"parts sum to the whole within {self._m.tolerance:.2%}"
        target = self._dim.table or self._m.table
        if grouped > total:
            return (
                f"grouping by {self._dim.name} gives {grouped:,.2f} against an "
                f"ungrouped total of {total:,.2f} — the traversal to {target} "
                f"inflates the metric by {delta:.2%}"
            )
        return (
            f"grouping by {self._dim.name} accounts for {grouped:,.2f} of "
            f"{total:,.2f} — {total - grouped:,.2f} ({delta:.2%}) disappears "
            f"in the traversal to {target}"
        )


class NullMass:
    """CON-02 — how much of the metric has no value for this dimension."""

    severity = Severity.WARN

    def __init__(
        self, metric: Metric, dimension: Dimension, sql: MetricSQL, threshold: float
    ) -> None:
        self._m, self._dim, self._sql = metric, dimension, sql
        self._threshold = threshold
        self.id = "CON-02"
        self.subject = f"{metric.name} by {dimension.name}"

    def run(self, ctx: CheckContext) -> CheckResult:
        rows = ctx.fetch(self._sql.grouped(self._dim, ctx.window))
        total = sum(float(v) for _, v in rows if v is not None)
        unattributed = sum(float(v) for k, v in rows if k is None and v is not None)
        share = unattributed / total if total else 0.0
        violated = share > self._threshold
        return CheckResult(
            self.id,
            self.subject,
            verdict(violated, self.severity),
            f"{share:.2%} of {self._m.name} has no {self._dim.name}"
            + (f" ({unattributed:,.2f} unattributed)" if violated else ""),
            observed=share,
            expected=self._threshold,
            delta=share,
        )


class FilterMass:
    """CON-03 — how much the metric's own filter removes."""

    severity = Severity.WARN

    def __init__(self, metric: Metric, sql: MetricSQL, threshold: float) -> None:
        self._m, self._sql, self._threshold = metric, sql, threshold
        self.id = "CON-03"
        self.subject = metric.name

    def run(self, ctx: CheckContext) -> CheckResult:
        rows = ctx.fetch(self._sql.filter_mass(ctx.window))
        kept, before = (float(rows[0][0] or 0), float(rows[0][1] or 0))
        removed = 1 - (kept / before) if before else 0.0
        violated = removed > self._threshold
        return CheckResult(
            self.id,
            self.subject,
            verdict(violated, self.severity),
            f"filter `{self._m.where}` removes {removed:.2%} of rows "
            f"({before - kept:,.0f} of {before:,.0f})",
            observed=removed,
            expected=self._threshold,
            delta=removed,
        )


class RowConservation:
    """CON-04 — a traversal must not multiply the base table (fan-out)."""

    severity = Severity.BLOCK

    def __init__(self, metric: Metric, join: Join, sql: MetricSQL) -> None:
        self._m, self._join, self._sql = metric, join, sql
        self.id = "CON-04"
        self.subject = f"{metric.name} -> {join.table}"

    def run(self, ctx: CheckContext) -> CheckResult:
        rows = ctx.fetch(self._sql.row_counts(self._join, ctx.window))
        base, joined = float(rows[0][0] or 0), float(rows[0][1] or 0)
        violated = joined > base
        ratio = (joined / base) if base else 1.0
        return CheckResult(
            self.id,
            self.subject,
            verdict(violated, self.severity),
            self._detail(base, joined, ratio, violated),
            observed=joined,
            expected=base,
            delta=ratio - 1,
        )

    def _detail(self, base: float, joined: float, ratio: float, bad: bool) -> str:
        if not bad:
            return f"traversal to {self._join.table} is many-to-one ({base:,.0f} rows)"
        return (
            f"fan-out: joining {self._join.table} turns {base:,.0f} rows into "
            f"{joined:,.0f} ({ratio:.2f}x) — every additive measure over this "
            f"path is overstated"
        )
