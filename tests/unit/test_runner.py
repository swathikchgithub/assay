"""Runner behaviour: isolation, caching, and the CI exit code."""

from assay.invariants.base import CheckContext, CheckResult, Severity, Status
from assay.engine.adapter import Query
from assay.engine.sql import Window
from assay.run.runner import RunSummary, run, sort_for_report
from tests.conftest import NOW, FakeAdapter


class Exploding:
    id, subject, severity = "CON-01", "revenue", Severity.BLOCK

    def run(self, ctx: CheckContext) -> CheckResult:
        raise RuntimeError("column does not exist")


def test_a_broken_check_is_reported_not_fatal(contracts, monkeypatch):
    monkeypatch.setattr("assay.run.runner.generate", lambda *a, **k: [Exploding()])
    summary = run(contracts, FakeAdapter({}), Window())
    assert summary.results[0].status is Status.FAIL
    assert "column does not exist" in summary.results[0].detail


def test_exit_code_is_non_zero_only_when_a_block_check_fails():
    fail = CheckResult("CON-01", "m", Status.FAIL, "")
    warn = CheckResult("CON-02", "m", Status.WARN, "")
    assert RunSummary("r", NOW, (warn,), 0, 0.0).exit_code == 0
    assert RunSummary("r", NOW, (fail, warn), 0, 0.0).exit_code == 1


def test_repeated_queries_are_scanned_once():
    """CON-01 and CON-02 legitimately need the same grouped query."""
    adapter = FakeAdapter({"SELECT 1": [(1,)]})
    ctx = CheckContext(adapter=adapter, window=Window(), now=NOW)
    ctx.fetch(Query("SELECT 1"))
    ctx.fetch(Query("SELECT 1"))
    assert ctx.scans == 1
    assert len(adapter.calls) == 1


def test_report_order_is_most_severe_first():
    results = [
        CheckResult("X", "a", Status.PASS, ""),
        CheckResult("Y", "b", Status.FAIL, ""),
        CheckResult("Z", "c", Status.WARN, ""),
    ]
    assert [r.status for r in sort_for_report(results)] == [
        Status.FAIL,
        Status.WARN,
        Status.PASS,
    ]
