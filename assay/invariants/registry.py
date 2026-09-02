"""Generation of the check suite from the contract set.

Nothing here is hand-written per customer: a metric contract implies its own
invariants. Adding a class means adding a generator, not editing the runner
(Open/Closed), and the guards below matter — generating CON-01 for a distinct
count would manufacture a permanent false positive and train people to ignore
the report.
"""

from __future__ import annotations

from dataclasses import dataclass

from assay.contracts.models import Additivity, ContractSet, Metric
from assay.engine.adapter import Dialect
from assay.engine.sql import MetricSQL
from assay.invariants.base import Invariant
from assay.invariants.conservation import (
    DecompositionSum,
    FilterMass,
    NullMass,
    RowConservation,
)
from assay.invariants.identity import (
    CrossGrainConsistency,
    DeclaredIdentity,
    DerivedIdentity,
    rollup_is_defined,
)
from assay.invariants.temporal import Discontinuity, Envelope, Freshness, Restatement


@dataclass(frozen=True)
class Thresholds:
    """Tunables that are not properties of an individual metric."""

    null_mass: float = 0.01
    filter_mass: float = 0.50
    envelope_k: float = 4.0
    discontinuity_k: float = 5.0
    min_observations: int = 6


def generate(
    contracts: ContractSet,
    dialect: Dialect,
    thresholds: Thresholds = Thresholds(),
) -> list[Invariant]:
    """Every invariant implied by the contract set.

    Time: O(metrics x dimensions). Space: O(invariants).
    """
    sql_by_metric = {m.name: MetricSQL(m, dialect) for m in contracts.metrics}
    invariants: list[Invariant] = []
    for metric in contracts.metrics:
        sql = sql_by_metric[metric.name]
        invariants.extend(_conservation(metric, sql, thresholds))
        invariants.extend(_identity(metric, sql, sql_by_metric))
        invariants.extend(_temporal(metric, sql, thresholds))
    invariants.extend(
        DeclaredIdentity(identity, sql_by_metric) for identity in contracts.identities
    )
    return invariants


def _conservation(
    metric: Metric, sql: MetricSQL, thresholds: Thresholds
) -> list[Invariant]:
    out: list[Invariant] = []
    if metric.additivity is Additivity.ADDITIVE:
        # Summing groups is only meaningful for a metric that may be summed.
        for dim in metric.dimensions:
            out.append(DecompositionSum(metric, dim, sql))
            out.append(NullMass(metric, dim, sql, thresholds.null_mass))
    if metric.where:
        out.append(FilterMass(metric, sql, thresholds.filter_mass))
    out.extend(
        RowConservation(metric, join, sql)
        for join in metric.joins
        if not join.required
    )
    return out


def _identity(
    metric: Metric, sql: MetricSQL, sql_by_metric: dict[str, MetricSQL]
) -> list[Invariant]:
    out: list[Invariant] = []
    if metric.derived:
        out.append(DerivedIdentity(metric, sql_by_metric))
    if rollup_is_defined(metric):
        out.append(CrossGrainConsistency(metric, sql))
    return out


def _temporal(
    metric: Metric, sql: MetricSQL, thresholds: Thresholds
) -> list[Invariant]:
    out: list[Invariant] = [
        Restatement(metric, sql),
        Envelope(metric, sql, thresholds.envelope_k, thresholds.min_observations),
        Discontinuity(
            metric, sql, thresholds.discontinuity_k, thresholds.min_observations
        ),
    ]
    if metric.freshness_sla_hours:
        out.insert(0, Freshness(metric, sql))
    return out
