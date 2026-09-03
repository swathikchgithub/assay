"""Executing a plan into an answer that can be argued with.

A number on its own is what this project exists to distrust. What comes back
here is the number plus the checks that ran against the metric and the paths
the plan used — so a reader can see why they may or may not believe it.

Only checks that can run in the time a person waits are included. The scheduled
family (restatement, envelope, identity) is reported from the last nightly run
rather than recomputed, because a proof card that takes a minute is not one
anybody reads.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Optional, Sequence

from assay.contracts.models import ContractSet, Grain, Metric
from assay.engine.adapter import Dialect, WarehouseAdapter
from assay.engine.sql import MetricSQL, Predicate, Window
from assay.invariants.base import CheckContext, CheckResult, Invariant, Status
from assay.invariants.registry import Thresholds, generate
from assay.nlq.plan import AbsoluteTime, Calendar, Query, Refusal, RelativeTime

# Checks fast enough to run while somebody waits.
LIVE_RULES = ("CON-01", "CON-02", "CON-03", "CON-04", "TMP-01")


@dataclass(frozen=True)
class Result:
    metric: str
    dimension: Optional[str]
    value: Optional[float]
    rows: tuple[tuple[Optional[str], float], ...]
    sql: str


@dataclass(frozen=True)
class Answer:
    plan: Query
    window: Window
    results: tuple[Result, ...] = ()
    checks: tuple[CheckResult, ...] = ()
    refusals: tuple[Refusal, ...] = ()
    duration_s: float = 0.0
    scans: int = 0

    @property
    def trustworthy(self) -> bool:
        """No blocking check failed. Warnings still render — they are caveats."""
        return bool(self.results) and not any(
            c.status is Status.FAIL for c in self.checks
        )


def execute(
    plan: Query,
    contracts: ContractSet,
    adapter: WarehouseAdapter,
    as_of: Optional[Any] = None,
    thresholds: Thresholds = Thresholds(),
) -> Answer:
    """Run a plan the gate has already accepted, and check the result."""
    started = time.monotonic()
    now = as_of or adapter.now()
    window = resolve_window(plan, now)
    if isinstance(window, Refusal):
        return Answer(plan=plan, window=Window(), refusals=(window,))
    if unsupported := _unsupported(plan):
        return Answer(plan=plan, window=window, refusals=(unsupported,))

    ctx = CheckContext(adapter=adapter, window=window, now=now)
    results = tuple(
        _run_metric(contracts.metric(name), plan, contracts, adapter.dialect, ctx)
        for name in plan.select
    )
    checks = _live_checks(plan, contracts, adapter.dialect, thresholds, ctx)
    return Answer(
        plan=plan,
        window=window,
        results=results,
        checks=checks,
        duration_s=time.monotonic() - started,
        scans=ctx.scans,
    )


# ---- execution ----------------------------------------------------------------


def _run_metric(
    metric: Metric, plan: Query, contracts: ContractSet, dialect: Dialect,
    ctx: CheckContext,
) -> Result:
    sql = MetricSQL(metric, dialect)
    predicates = _predicates(metric, plan)
    if plan.by:
        dimension = metric.dimension(plan.by[0])
        query = sql.grouped(dimension, ctx.window, predicates)
        rows = tuple(
            (None if k is None else str(k), float(v))
            for k, v in ctx.fetch(query)
            if v is not None
        )
        return Result(metric.name, dimension.name, sum(v for _, v in rows), rows, query.sql)
    query = sql.total(ctx.window, predicates)
    fetched = ctx.fetch(query)
    value = float(fetched[0][0]) if fetched and fetched[0][0] is not None else None
    return Result(metric.name, None, value, (), query.sql)


def _predicates(metric: Metric, plan: Query) -> list[Predicate]:
    return [
        Predicate(metric.dimension(f.dimension), f.op.value, f.value) for f in plan.where
    ]


def _unsupported(plan: Query) -> Optional[Refusal]:
    if len(plan.by) > 1:
        return Refusal(
            rule="NLQ-06",
            reason="grouping by more than one dimension is not supported yet",
            repair=f"Ask for one slice at a time — try {plan.by[0]!r}.",
            concept="multi-dimensional grouping",
        )
    return None


# ---- which checks to run ------------------------------------------------------


def _live_checks(
    plan: Query, contracts: ContractSet, dialect: Dialect, thresholds: Thresholds,
    ctx: CheckContext,
) -> tuple[CheckResult, ...]:
    """The generated suite, narrowed to this plan's metrics and paths.

    Reuses `registry.generate` rather than inventing a parallel set, so a check
    on the proof card is the same check the nightly run reports.
    """
    relevant = [
        inv
        for inv in generate(contracts, dialect, thresholds)
        if inv.id in LIVE_RULES and _applies(inv, plan, contracts)
    ]
    return tuple(_safely(inv, ctx) for inv in relevant)


def _applies(invariant: Invariant, plan: Query, contracts: ContractSet) -> bool:
    """Only checks that bear on *this* answer.

    A fan-out through a path the plan never traverses is a true statement about
    the metric and an irrelevant one about the number in front of you. Putting
    it on the card teaches people that the caveats are noise.
    """
    subject = invariant.subject
    metric = subject.split(" by ")[0].split(" -> ")[0]
    if metric not in plan.select:
        return False
    if " by " in subject:
        return subject.split(" by ", 1)[1] in plan.dimensions_used
    if " -> " in subject:
        return subject.split(" -> ", 1)[1] in _tables_traversed(plan, contracts, metric)
    return True


def _tables_traversed(plan: Query, contracts: ContractSet, metric_name: str) -> set[str]:
    """Tables this plan actually reaches, via a grouped or filtered dimension."""
    metric = contracts.metric(metric_name)
    tables = set()
    for name in plan.dimensions_used:
        dimension = next((d for d in metric.dimensions if d.name == name), None)
        if dimension is not None and dimension.table:
            tables.add(dimension.table)
    return tables


def _safely(invariant: Invariant, ctx: CheckContext) -> CheckResult:
    try:
        return invariant.run(ctx)
    except Exception as exc:  # noqa: BLE001 - a broken check is reported, not fatal
        return CheckResult(
            invariant.id, invariant.subject, Status.FAIL,
            f"check could not run: {type(exc).__name__}: {exc}",
        )


# ---- time ---------------------------------------------------------------------


def resolve_window(plan: Query, now: Any) -> Any:
    """Plan time -> a concrete half-open window, or a refusal."""
    spec = plan.time
    if getattr(spec, "kind", "all") == "all":
        return Window()
    if isinstance(spec, AbsoluteTime):
        return Window(start=spec.start, end=spec.end)
    assert isinstance(spec, RelativeTime)
    if spec.calendar is Calendar.FISCAL:
        return Refusal(
            rule="NLQ-07",
            reason="no fiscal calendar is configured for this project",
            repair="Ask for a Gregorian period, or declare a fiscal calendar.",
            concept="calendar:fiscal",
        )
    today = now.date() if hasattr(now, "date") else now
    start = _period_start(today, spec.anchor, spec.offset)
    return Window(start=start, end=_period_start(today, spec.anchor, spec.offset + 1))


def _period_start(today: date, anchor: Grain, offset: int) -> date:
    if anchor is Grain.DAY:
        return today + timedelta(days=offset)
    if anchor is Grain.WEEK:
        return today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    if anchor is Grain.MONTH:
        return _shift_months(date(today.year, today.month, 1), offset)
    if anchor is Grain.QUARTER:
        first = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
        return _shift_months(first, offset * 3)
    return date(today.year + offset, 1, 1)


def _shift_months(start: date, months: int) -> date:
    total = (start.year * 12 + start.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)
