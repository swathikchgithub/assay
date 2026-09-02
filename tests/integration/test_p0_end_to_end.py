"""The test that decides whether P0 is worth anything.

Seeds a warehouse containing seven defects an analytics team actually ships,
runs the real suite against it through the real adapter, and asserts each one
is found and named. Then restates a closed month and asserts the second run
notices the past changed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from assay.contracts.sources import YamlSource
from assay.engine.duckdb_adapter import DuckDBAdapter
from assay.engine.sql import Window
from assay.invariants.base import Status
from assay.run.history import History
from assay.run.runner import RunSummary, run
from demo.seed import AS_OF, seed

CONTRACTS = Path(__file__).parents[2] / "demo" / "contracts.yml"
DAYS = 300  # long enough to include the promo window and ten closed months


def _run(database: Path, history: History, as_of: datetime) -> RunSummary:
    with DuckDBAdapter(str(database), as_of=as_of) as adapter:
        return run(
            YamlSource(CONTRACTS).load(),
            adapter,
            Window(start=(as_of - timedelta(days=DAYS)).date()),
            history=history,
        )


def _find(summary: RunSummary, invariant_id: str, subject: str):
    matches = [
        r for r in summary.results
        if r.invariant_id == invariant_id and r.subject == subject
    ]
    assert matches, f"no {invariant_id} check for {subject!r}"
    return matches[0]


@pytest.fixture(scope="module")
def first_run(tmp_path_factory) -> tuple[RunSummary, Path, Path]:
    directory = tmp_path_factory.mktemp("assay")
    database = directory / "demo.duckdb"
    history_path = directory / "history.db"
    seed(database, days=DAYS)
    history = History(history_path)
    summary = _run(database, history, AS_OF.replace(tzinfo=timezone.utc))
    history.close()
    return summary, database, history_path


def test_the_lossy_region_lookup_is_caught(first_run):
    summary, _, _ = first_run
    result = _find(summary, "CON-01", "net_revenue by region")
    assert result.status is Status.FAIL
    assert "disappears in the traversal to regions" in result.detail


def test_the_unattributed_segment_share_is_reported(first_run):
    summary, _, _ = first_run
    result = _find(summary, "CON-02", "net_revenue by segment")
    assert result.status is Status.WARN
    assert 0.03 < result.observed < 0.09


def test_the_order_item_fan_out_is_caught(first_run):
    summary, _, _ = first_run
    result = _find(summary, "CON-04", "net_revenue -> order_items")
    assert result.status is Status.FAIL
    assert result.observed > result.expected


def test_the_double_counted_promo_breaks_the_identity(first_run):
    summary, _, _ = first_run
    result = _find(summary, "IDN-01", "net_is_gross_less_discounts")
    assert result.status is Status.FAIL


def test_distinct_users_declared_additive_is_caught(first_run):
    summary, _, _ = first_run
    result = _find(summary, "IDN-03", "active_users")
    assert result.status is Status.FAIL
    assert "does not roll up that way" in result.detail


def test_the_stalled_ticket_pipeline_is_caught(first_run):
    summary, _, _ = first_run
    result = _find(summary, "TMP-01", "open_tickets")
    assert result.status is Status.WARN
    assert result.observed == pytest.approx(40.0, abs=0.5)


def test_a_healthy_metric_is_not_flagged(first_run):
    """Credibility depends on the clean metrics staying clean."""
    summary, _, _ = first_run
    assert _find(summary, "IDN-03", "net_revenue").status is Status.PASS
    assert _find(summary, "TMP-03", "gross_revenue").status is Status.SKIP


def test_the_first_run_exits_non_zero_for_ci(first_run):
    summary, _, _ = first_run
    assert summary.exit_code == 1


def test_a_backfill_of_a_closed_month_is_detected(first_run):
    summary, database, history_path = first_run
    assert _find(summary, "TMP-03", "net_revenue").status is Status.SKIP

    seed(database, backfill=True)
    history = History(history_path)
    second = _run(database, history, AS_OF.replace(tzinfo=timezone.utc) + timedelta(days=1))
    history.close()

    restated = _find(second, "TMP-03", "net_revenue")
    assert restated.status is Status.FAIL
    assert "2026-04" in restated.detail


def test_an_untouched_metric_reports_no_restatement(first_run):
    """The backfill only touched orders, so the events metric must stay quiet."""
    _, database, history_path = first_run
    history = History(history_path)
    third = _run(database, history, AS_OF.replace(tzinfo=timezone.utc) + timedelta(days=2))
    history.close()
    assert _find(third, "TMP-03", "active_users").status is Status.PASS
