"""Orchestration: generate the suite, run it, summarise.

The runner knows nothing about which invariants exist. It receives them from
the registry and treats each as an opaque `Invariant`, so a new check class
never touches this file.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from assay.contracts.models import ContractSet
from assay.engine.adapter import WarehouseAdapter
from assay.engine.sql import Window
from assay.invariants.base import (
    CheckContext,
    CheckResult,
    Invariant,
    SnapshotStore,
    Status,
)
from assay.invariants.registry import Thresholds, generate


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    ran_at: datetime
    results: tuple[CheckResult, ...]
    scans: int
    duration_s: float

    def by_status(self, status: Status) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.status is status)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return self.by_status(Status.FAIL)

    @property
    def warnings(self) -> tuple[CheckResult, ...]:
        return self.by_status(Status.WARN)

    @property
    def exit_code(self) -> int:
        """Non-zero when a block-severity invariant failed, so CI can gate."""
        return 1 if self.failures else 0

    @property
    def headline(self) -> str:
        return (
            f"{len(self.failures)} failed, {len(self.warnings)} warned, "
            f"{len(self.by_status(Status.PASS))} passed, "
            f"{len(self.by_status(Status.SKIP))} skipped"
        )


def run(
    contracts: ContractSet,
    adapter: WarehouseAdapter,
    window: Window,
    history: Optional[SnapshotStore] = None,
    thresholds: Thresholds = Thresholds(),
) -> RunSummary:
    """Generate and execute the whole suite against one warehouse."""
    started = time.monotonic()
    ran_at = adapter.now()
    ctx = CheckContext(adapter=adapter, window=window, now=ran_at, history=history)
    invariants = generate(contracts, adapter.dialect, thresholds)
    results = tuple(_safely(inv, ctx) for inv in invariants)
    return RunSummary(
        run_id=uuid.uuid4().hex[:12],
        ran_at=ran_at,
        results=results,
        scans=ctx.scans,
        duration_s=time.monotonic() - started,
    )


def _safely(invariant: Invariant, ctx: CheckContext) -> CheckResult:
    """A check that cannot run is reported, never fatal to the rest of the suite."""
    try:
        return invariant.run(ctx)
    except Exception as exc:  # noqa: BLE001 - a bad contract must not stop the run
        return CheckResult(
            invariant.id,
            invariant.subject,
            Status.FAIL,
            f"check could not run: {type(exc).__name__}: {exc}",
        )


def sort_for_report(results: Sequence[CheckResult]) -> list[CheckResult]:
    """Most severe first, then by invariant id for a stable diff between runs."""
    order = {Status.FAIL: 0, Status.WARN: 1, Status.SKIP: 2, Status.PASS: 3}
    return sorted(results, key=lambda r: (order[r.status], r.invariant_id, r.subject))
