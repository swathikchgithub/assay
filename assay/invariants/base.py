"""Invariant protocol and result types.

An invariant is a machine-checkable assertion about a metric or an identity.
Almost all of them are *generated* from the contract set rather than written,
which is what stops the suite decaying the way a golden-query file does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Protocol

from assay.engine.adapter import Query, WarehouseAdapter
from assay.engine.sql import Window


class Severity(str, Enum):
    BLOCK = "block"
    WARN = "warn"
    NOTE = "note"


class Status(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class CheckResult:
    invariant_id: str
    subject: str
    status: Status
    detail: str
    observed: Optional[float] = None
    expected: Optional[float] = None
    delta: Optional[float] = None

    @property
    def violated(self) -> bool:
        return self.status in (Status.FAIL, Status.WARN)


@dataclass
class CheckContext:
    """Everything an invariant may reach for, and nothing else.

    `fetch` memoizes on (sql, params) for the life of one run. Several
    invariants legitimately need the same scan — CON-01 and CON-02 share a
    grouped query — and paying for it twice is the query amplification the
    spec flags as the fastest way to get uninstalled.
    """

    adapter: WarehouseAdapter
    window: Window
    now: datetime
    history: Optional[SnapshotStore] = None
    _cache: dict[tuple[str, tuple[Any, ...]], list[tuple[Any, ...]]] = field(
        default_factory=dict, repr=False
    )
    scans: int = 0

    def fetch(self, query: Query) -> list[tuple[Any, ...]]:
        key = (query.sql, query.params)
        if key not in self._cache:
            self._cache[key] = self.adapter.fetch(query)
            self.scans += 1
        return self._cache[key]


class SnapshotStore(Protocol):
    """Prior observations of a metric's per-period values.

    Declared here rather than imported from the run layer so the invariants
    package stays independent of how history happens to be persisted.
    """

    def previous(self, metric: str) -> dict[str, float]: ...

    def record(
        self, metric: str, series: dict[str, float], observed_at: datetime
    ) -> None: ...


class Invariant(Protocol):
    id: str
    subject: str
    severity: Severity

    def run(self, ctx: CheckContext) -> CheckResult: ...


def verdict(violated: bool, severity: Severity) -> Status:
    """Map a violation onto a status according to the invariant's severity."""
    if not violated:
        return Status.PASS
    return Status.FAIL if severity is Severity.BLOCK else Status.WARN


def relative_delta(observed: float, expected: float) -> float:
    """Relative difference, falling back to absolute when expected is zero."""
    if expected == 0:
        return abs(observed)
    return abs(observed - expected) / abs(expected)
