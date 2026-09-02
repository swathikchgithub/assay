"""History is what makes the past comparable across nightly runs."""

from datetime import datetime

from assay.invariants.base import CheckResult, Status
from assay.run.history import History

NOW = datetime(2026, 9, 1, 9, 0)


def test_an_unseen_metric_has_no_baseline(tmp_path):
    history = History(tmp_path / "h.db")
    assert history.previous("revenue") == {}


def test_a_recorded_series_reads_back(tmp_path):
    history = History(tmp_path / "h.db")
    history.record("revenue", {"2026-01": 100.0}, NOW)
    assert history.previous("revenue") == {"2026-01": 100.0}


def test_recording_a_period_again_overwrites_it(tmp_path):
    history = History(tmp_path / "h.db")
    history.record("revenue", {"2026-01": 100.0}, NOW)
    history.record("revenue", {"2026-01": 140.0}, NOW)
    assert history.previous("revenue") == {"2026-01": 140.0}


def test_history_survives_reopening_the_file(tmp_path):
    path = tmp_path / "h.db"
    first = History(path)
    first.record("revenue", {"2026-01": 100.0}, NOW)
    first.close()
    assert History(path).previous("revenue") == {"2026-01": 100.0}


def test_check_results_are_logged_per_run(tmp_path):
    history = History(tmp_path / "h.db")
    history.record_checks(
        "run-1", [CheckResult("CON-01", "revenue", Status.FAIL, "broken")], NOW
    )
    assert list(history.runs()) == ["run-1"]
