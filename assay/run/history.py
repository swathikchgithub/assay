"""Observation history — the baseline TMP-03 compares against.

SQLite because P0 runs as a nightly job next to the warehouse, not as a
service. The schema is the subset of spec section 6.2 that restatement
detection needs; the answer and view tables arrive with P1.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from assay.invariants.base import CheckResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_snapshot (
    metric      TEXT NOT NULL,
    period      TEXT NOT NULL,
    value       DOUBLE NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (metric, period)
);
CREATE TABLE IF NOT EXISTS check_run (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    invariant_id TEXT NOT NULL,
    subject      TEXT NOT NULL,
    status       TEXT NOT NULL,
    detail       TEXT NOT NULL,
    observed     DOUBLE,
    expected     DOUBLE,
    delta        DOUBLE,
    ran_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS check_run_subject
    ON check_run (invariant_id, subject, ran_at DESC);
"""


class History:
    """Implements `SnapshotStore` plus the check-result log."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)

    def previous(self, metric: str) -> dict[str, float]:
        """Last recorded series for a metric. Time: O(periods) via the PK index."""
        rows = self._conn.execute(
            "SELECT period, value FROM metric_snapshot WHERE metric = ?", (metric,)
        ).fetchall()
        return {period: float(value) for period, value in rows}

    def record(
        self, metric: str, series: dict[str, float], observed_at: datetime
    ) -> None:
        self._conn.executemany(
            "INSERT INTO metric_snapshot (metric, period, value, observed_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (metric, period) "
            "DO UPDATE SET value = excluded.value, observed_at = excluded.observed_at",
            [
                (metric, period, value, observed_at.isoformat())
                for period, value in series.items()
            ],
        )
        self._conn.commit()

    def record_checks(
        self, run_id: str, results: Sequence[CheckResult], ran_at: datetime
    ) -> None:
        self._conn.executemany(
            "INSERT INTO check_run (run_id, invariant_id, subject, status, detail, "
            "observed, expected, delta, ran_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    r.invariant_id,
                    r.subject,
                    r.status.value,
                    r.detail,
                    r.observed,
                    r.expected,
                    r.delta,
                    ran_at.isoformat(),
                )
                for r in results
            ],
        )
        self._conn.commit()

    def runs(self) -> Iterable[str]:
        return [
            row[0]
            for row in self._conn.execute(
                "SELECT DISTINCT run_id FROM check_run ORDER BY id"
            )
        ]

    def close(self) -> None:
        self._conn.close()
