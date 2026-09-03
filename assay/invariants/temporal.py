"""Class V — Temporal. What the metric's own history says about it.

TMP-03 is the reason this class exists. Every warehouse recomputes the past
constantly — late-arriving rows, backfills, restated source systems — and no
existing tool tells anyone. It is the check that makes phase P1's recall
service possible, and on its own it is the most surprising thing P0 reports.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional, Union

from assay.contracts.models import Grain, Metric
from assay.engine.sql import MetricSQL
from assay.invariants.base import (
    CheckContext,
    CheckResult,
    Severity,
    Status,
    relative_delta,
    verdict,
)
from assay.invariants.stats import envelope


class Freshness:
    """TMP-01 — the newest row is no older than the metric's SLA."""

    severity = Severity.WARN

    def __init__(self, metric: Metric, sql: MetricSQL) -> None:
        self._m, self._sql = metric, sql
        self.id = "TMP-01"
        self.subject = metric.name

    def run(self, ctx: CheckContext) -> CheckResult:
        rows = ctx.fetch(self._sql.max_time())
        latest = rows[0][0] if rows else None
        if latest is None:
            return CheckResult(self.id, self.subject, Status.FAIL, "table is empty")
        lag_hours = self._lag_hours(latest, ctx.now)
        sla = float(self._m.freshness_sla_hours or 0)
        violated = lag_hours > sla
        return CheckResult(
            self.id,
            self.subject,
            verdict(violated, self.severity),
            f"newest row is {lag_hours:.1f}h old against a {sla:.0f}h SLA",
            observed=lag_hours,
            expected=sla,
            delta=lag_hours - sla,
        )

    @staticmethod
    def _lag_hours(latest: datetime, now: datetime) -> float:
        """Warehouses store naive UTC; a caller's clock may be aware. Normalise both."""
        return max((_as_utc(now) - _as_utc(latest)).total_seconds() / 3600.0, 0.0)


class Envelope:
    """TMP-02 — the newest closed period sits inside its own history."""

    severity = Severity.WARN

    def __init__(
        self, metric: Metric, sql: MetricSQL, k: float = 4.0, min_observations: int = 6
    ) -> None:
        self._m, self._sql, self._k = metric, sql, k
        self._min = min_observations
        self.id = "TMP-02"
        self.subject = metric.name

    def run(self, ctx: CheckContext) -> CheckResult:
        series = _closed_periods(ctx, self._sql)
        if len(series) < self._min + 1:
            return CheckResult(
                self.id,
                self.subject,
                Status.SKIP,
                f"{len(series)} closed periods, needs {self._min + 1} to calibrate",
            )
        periods = sorted(series)
        latest, history = periods[-1], [series[p] for p in periods[:-1]]
        low, high = envelope(history, self._k)
        value = series[latest]
        violated = not (low <= value <= high)
        return CheckResult(
            self.id,
            self.subject,
            verdict(violated, self.severity),
            f"{latest}: {value:,.2f} against a robust band of "
            f"[{low:,.2f}, {high:,.2f}]",
            observed=value,
            expected=(low + high) / 2,
            delta=abs(value - (low + high) / 2),
        )


class Restatement:
    """TMP-03 — a closed period's value has changed since it was last observed.

    Records the current series as it goes, so the next run has a baseline. That
    write is the check's whole purpose, not a side effect: without it there is
    nothing to compare against and the past keeps changing unnoticed.
    """

    severity = Severity.BLOCK

    def __init__(self, metric: Metric, sql: MetricSQL) -> None:
        self._m, self._sql = metric, sql
        self.id = "TMP-03"
        self.subject = metric.name

    def run(self, ctx: CheckContext) -> CheckResult:
        if ctx.history is None:
            return CheckResult(self.id, self.subject, Status.SKIP, "no history store")
        current = _closed_periods(ctx, self._sql)
        baseline = ctx.history.previous(self._m.name)
        ctx.history.record(self._m.name, current, ctx.now)
        if not baseline:
            return CheckResult(
                self.id, self.subject, Status.SKIP, "first run — baseline recorded"
            )
        moved = self._moved(current, baseline)
        return self._result(moved, baseline)

    def _moved(
        self, current: dict[str, float], baseline: dict[str, float]
    ) -> list[tuple[str, float, float]]:
        return [
            (period, baseline[period], current[period])
            for period in sorted(set(current) & set(baseline))
            if relative_delta(current[period], baseline[period]) > self._m.tolerance
        ]

    def _result(
        self, moved: list[tuple[str, float, float]], baseline: dict[str, float]
    ) -> CheckResult:
        if not moved:
            return CheckResult(
                self.id,
                self.subject,
                Status.PASS,
                f"all {len(baseline)} previously observed periods are unchanged",
            )
        period, was, now = max(moved, key=lambda m: relative_delta(m[2], m[1]))
        delta = relative_delta(now, was)
        return CheckResult(
            self.id,
            self.subject,
            verdict(True, self.severity),
            f"{len(moved)} closed period(s) changed since the last run — "
            f"{period} was {was:,.2f}, is now {now:,.2f} ({delta:.2%})",
            observed=now,
            expected=was,
            delta=delta,
        )


class Discontinuity:
    """TMP-04 — a step change unlike any other step in the series."""

    severity = Severity.NOTE

    def __init__(
        self, metric: Metric, sql: MetricSQL, k: float = 5.0, min_observations: int = 6
    ) -> None:
        self._m, self._sql, self._k = metric, sql, k
        self._min = min_observations
        self.id = "TMP-04"
        self.subject = metric.name

    def run(self, ctx: CheckContext) -> CheckResult:
        series = _closed_periods(ctx, self._sql)
        periods = sorted(series)
        if len(periods) < self._min + 2:
            return CheckResult(self.id, self.subject, Status.SKIP, "series too short")
        steps = [series[b] - series[a] for a, b in zip(periods, periods[1:])]
        low, high = envelope(steps[:-1], self._k)
        latest = steps[-1]
        violated = not (low <= latest <= high)
        return CheckResult(
            self.id,
            self.subject,
            verdict(violated, self.severity),
            f"{periods[-1]}: step of {latest:,.2f} against typical steps in "
            f"[{low:,.2f}, {high:,.2f}]",
            observed=latest,
            expected=(low + high) / 2,
            delta=abs(latest - (low + high) / 2),
        )


def _closed_periods(ctx: CheckContext, sql: MetricSQL) -> dict[str, float]:
    """Monthly series covering only periods the window fully contains.

    Both edges have to go. The current month is still accumulating, so
    including it makes every run report a collapse on the first. The oldest
    month is clipped by a rolling lookback, so its value shifts a little every
    night — which TMP-03 would otherwise report as a restatement, and a check
    that cries wolf on a calendar boundary is a check nobody reads.
    """
    open_period = ctx.now.strftime("%Y-%m")
    first_full = _first_full_period(ctx.window.start)
    return {
        str(period)[:7]: float(value)
        for period, value in ctx.fetch(sql.by_period(Grain.MONTH, ctx.window))
        if value is not None and first_full <= str(period)[:7] < open_period
    }


def _first_full_period(start: Optional[date]) -> str:
    """Earliest month the window covers in its entirety."""
    if start is None:
        return ""
    if start.day == 1:
        return start.strftime("%Y-%m")
    following = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return following.strftime("%Y-%m")


def _as_utc(value: Union[datetime, date]) -> datetime:
    """Normalise whatever the warehouse returned for a time column.

    A DATE column comes back as `datetime.date`, which has no `tzinfo` at all.
    Every table in the demo uses TIMESTAMP, so this only showed up the first
    time Assay ran against a real warehouse - TPCH's `o_orderdate` is a DATE,
    and freshness crashed rather than reporting.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
