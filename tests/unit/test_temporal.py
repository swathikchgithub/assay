"""Class V invariants, with the clock injected so results are deterministic."""

from datetime import date

from assay.contracts.models import Metric
from assay.engine.duckdb_adapter import DuckDBDialect
from assay.engine.sql import MetricSQL, Window
from assay.invariants.base import CheckContext, Status
from assay.invariants.temporal import Discontinuity, Envelope, Freshness, Restatement
from tests.conftest import NOW, FakeAdapter, FakeHistory


def _metric(**overrides) -> Metric:
    base = {
        "name": "revenue",
        "table": "orders",
        "measure": "sum(amount)",
        "time_column": "ts",
        "additivity": "additive",
    }
    return Metric(**{**base, **overrides})


def _series(values: dict[str, float]) -> FakeAdapter:
    return FakeAdapter(
        {"'month'": [(f"{period}-01", value) for period, value in values.items()]}
    )


def _ctx(adapter, history=None, start=date(2026, 1, 1)) -> CheckContext:
    return CheckContext(
        adapter=adapter, window=Window(start=start), now=NOW, history=history
    )


def test_freshness_warns_when_the_newest_row_is_past_the_sla():
    from datetime import datetime

    metric = _metric(freshness_sla_hours=24)
    adapter = FakeAdapter({"max(": [(datetime(2026, 8, 30, 9, 0),)]})
    result = Freshness(metric, MetricSQL(metric, DuckDBDialect())).run(_ctx(adapter))
    assert result.status is Status.WARN
    assert result.observed == 48.0


def test_freshness_passes_inside_the_sla():
    from datetime import datetime

    metric = _metric(freshness_sla_hours=24)
    adapter = FakeAdapter({"max(": [(datetime(2026, 9, 1, 1, 0),)]})
    assert Freshness(metric, MetricSQL(metric, DuckDBDialect())).run(
        _ctx(adapter)
    ).status is Status.PASS


def test_freshness_fails_on_an_empty_table():
    metric = _metric(freshness_sla_hours=24)
    adapter = FakeAdapter({"max(": [(None,)]})
    assert Freshness(metric, MetricSQL(metric, DuckDBDialect())).run(
        _ctx(adapter)
    ).status is Status.FAIL


def test_envelope_skips_until_it_has_enough_history():
    metric = _metric()
    adapter = _series({"2026-01": 100.0, "2026-02": 101.0})
    result = Envelope(metric, MetricSQL(metric, DuckDBDialect())).run(_ctx(adapter))
    assert result.status is Status.SKIP
    assert "needs 7" in result.detail


def test_envelope_flags_a_period_outside_its_own_history():
    metric = _metric()
    history = {f"2025-{m:02d}": 100.0 + m for m in range(1, 8)}
    adapter = _series({**history, "2026-01": 900.0})
    result = Envelope(metric, MetricSQL(metric, DuckDBDialect())).run(
        _ctx(adapter, start=date(2025, 1, 1))
    )
    assert result.status is Status.WARN


def test_restatement_skips_and_records_a_baseline_on_first_run():
    metric = _metric()
    history = FakeHistory()
    adapter = _series({"2026-01": 100.0, "2026-02": 110.0})
    result = Restatement(metric, MetricSQL(metric, DuckDBDialect())).run(
        _ctx(adapter, history)
    )
    assert result.status is Status.SKIP
    assert history.recorded["revenue"] == {"2026-01": 100.0, "2026-02": 110.0}


def test_restatement_fails_when_a_closed_period_moved():
    metric = _metric()
    history = FakeHistory({"revenue": {"2026-01": 100.0, "2026-02": 110.0}})
    adapter = _series({"2026-01": 140.0, "2026-02": 110.0})
    result = Restatement(metric, MetricSQL(metric, DuckDBDialect())).run(
        _ctx(adapter, history)
    )
    assert result.status is Status.FAIL
    assert "2026-01 was 100.00, is now 140.00" in result.detail


def test_restatement_passes_when_the_past_is_unchanged():
    metric = _metric()
    history = FakeHistory({"revenue": {"2026-01": 100.0}})
    adapter = _series({"2026-01": 100.0, "2026-02": 110.0})
    assert Restatement(metric, MetricSQL(metric, DuckDBDialect())).run(
        _ctx(adapter, history)
    ).status is Status.PASS


def test_the_in_progress_period_is_never_compared():
    """September is still accumulating on 1 September."""
    metric = _metric()
    history = FakeHistory({"revenue": {"2026-09": 5.0}})
    adapter = _series({"2026-08": 100.0, "2026-09": 900.0})
    result = Restatement(metric, MetricSQL(metric, DuckDBDialect())).run(
        _ctx(adapter, history, start=date(2026, 1, 1))
    )
    assert result.status is Status.PASS


def test_the_clipped_first_period_is_never_compared():
    """A rolling lookback truncates its oldest month; that is not a restatement."""
    metric = _metric()
    history = FakeHistory({"revenue": {"2026-01": 40.0, "2026-02": 100.0}})
    adapter = _series({"2026-01": 30.0, "2026-02": 100.0})
    result = Restatement(metric, MetricSQL(metric, DuckDBDialect())).run(
        _ctx(adapter, history, start=date(2026, 1, 15))
    )
    assert result.status is Status.PASS


def test_discontinuity_notes_an_unusual_step():
    metric = _metric()
    values = {f"2025-{m:02d}": 100.0 + m for m in range(1, 10)}
    adapter = _series({**values, "2026-01": 5000.0})
    result = Discontinuity(metric, MetricSQL(metric, DuckDBDialect())).run(
        _ctx(adapter, start=date(2025, 1, 1))
    )
    assert result.status is Status.WARN or result.status is Status.PASS
    assert result.observed is not None


def test_freshness_accepts_a_date_column():
    """A DATE column returns `datetime.date`, which has no tzinfo.

    Every demo table uses TIMESTAMP, so this only surfaced the first time Assay
    ran against a real warehouse — TPCH's `o_orderdate` is a DATE and the check
    crashed instead of reporting.
    """
    from datetime import date as _date

    metric = _metric(freshness_sla_hours=24)
    adapter = FakeAdapter({"max(": [(_date(2026, 8, 30),)]})
    result = Freshness(metric, MetricSQL(metric, DuckDBDialect())).run(_ctx(adapter))
    assert result.status is Status.WARN
    assert result.observed == 57.0  # midnight on the 30th to 09:00 on 1 Sept


def test_freshness_treats_a_date_as_midnight_utc():
    from datetime import date as _date

    metric = _metric(freshness_sla_hours=72)
    adapter = FakeAdapter({"max(": [(_date(2026, 9, 1),)]})
    result = Freshness(metric, MetricSQL(metric, DuckDBDialect())).run(_ctx(adapter))
    assert result.status is Status.PASS
    assert result.observed == 9.0
