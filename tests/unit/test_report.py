"""The report is the product's only surface in P0 — it has to lead with damage."""

from datetime import datetime

from assay.invariants.base import CheckResult, Status
from assay.run.report import markdown, slack_blocks
from assay.run.runner import RunSummary


def _summary(*results: CheckResult) -> RunSummary:
    return RunSummary(
        run_id="abc123",
        ran_at=datetime(2026, 9, 1, 9, 0),
        results=results,
        scans=4,
        duration_s=0.1,
    )


FAIL = CheckResult("CON-04", "revenue -> items", Status.FAIL, "fan-out: 2.5x")
WARN = CheckResult("CON-02", "revenue by region", Status.WARN, "6% unattributed")
PASS = CheckResult("IDN-03", "revenue", Status.PASS, "rolls up correctly")


def test_markdown_hides_passing_checks_by_default():
    out = markdown(_summary(FAIL, PASS))
    assert "fan-out" in out
    assert "rolls up correctly" not in out


def test_markdown_includes_passing_checks_when_asked():
    assert "rolls up correctly" in markdown(_summary(FAIL, PASS), include_passing=True)


def test_markdown_puts_failures_before_warnings():
    out = markdown(_summary(WARN, FAIL))
    assert out.index("## Failed") < out.index("## Warnings")


def test_markdown_says_so_when_nothing_is_wrong():
    assert "All checks passed." in markdown(_summary(PASS))


def test_slack_title_counts_failures():
    assert slack_blocks(_summary(FAIL, WARN))["text"] == "Assay: 1 metric checks failed"


def test_slack_omits_passing_checks():
    blocks = slack_blocks(_summary(FAIL, PASS))["blocks"]
    assert not any("rolls up correctly" in str(b) for b in blocks)


def test_slack_respects_the_block_limit():
    many = tuple(
        CheckResult("CON-01", f"m{i}", Status.FAIL, "broken") for i in range(30)
    )
    blocks = slack_blocks(_summary(*many), limit=5)["blocks"]
    assert len(blocks) == 7  # header + context + 5 findings
