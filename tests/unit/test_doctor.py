"""Doctor checks.

Each of these exists because a real setup failed in a way whose error named
the symptom and not the cause, so the assertions are mostly about whether the
`fix` text would actually get someone unstuck.
"""

from __future__ import annotations

import ssl
from datetime import datetime

import pytest

from assay.contracts.models import ContractSet, Metric
from assay.run import doctor
from assay.run.doctor import (
    Finding,
    check_case_policy,
    check_endpoint,
    check_objects,
    check_role,
    check_row_counts,
    check_session,
    render,
)
from tests.conftest import FakeAdapter

CATALOG = "information_schema.columns"


def _metric(**overrides) -> Metric:
    base = {
        "name": "revenue",
        "table": "ORDERS",
        "measure": "sum(amount)",
        "time_column": "ORDERED_AT",
        "additivity": "additive",
    }
    return Metric(**{**base, **overrides})


def _contracts(*metrics: Metric) -> ContractSet:
    return ContractSet(metrics=metrics or (_metric(),))


# ---- endpoint -----------------------------------------------------------


def test_a_valid_certificate_passes():
    assert check_endpoint("ORG-ACCOUNT", verifier=lambda host: None).ok


def test_a_mismatched_certificate_is_blocking_and_names_the_remedy():
    def bad(host):
        raise ssl.SSLCertVerificationError("hostname mismatch")

    finding = check_endpoint("XY12345.us-east-2", verifier=bad)
    assert finding.fatal and not finding.ok
    assert "SYSTEM$ALLOWLIST" in finding.fix


def test_an_unreachable_host_is_blocking():
    def unreachable(host):
        raise OSError("no route to host")

    assert check_endpoint("ORG-ACCOUNT", verifier=unreachable).fatal


def test_the_endpoint_host_is_derived_from_the_account():
    seen = []
    check_endpoint("ORG-ACCOUNT", verifier=seen.append)
    assert seen == ["ORG-ACCOUNT.snowflakecomputing.com"]


# ---- session and role ---------------------------------------------------


def test_session_reports_role_warehouse_and_target():
    adapter = FakeAdapter({"CURRENT_ROLE": [("ASSAY_RO", "WH", "PROD", "MART")]})
    finding = check_session(adapter)
    assert finding.ok and "ASSAY_RO @ WH · PROD.MART" in finding.detail


def test_no_selected_database_is_blocking():
    adapter = FakeAdapter({"CURRENT_ROLE": [("ASSAY_RO", "WH", None, None)]})
    finding = check_session(adapter)
    assert finding.fatal and "SNOWFLAKE_DATABASE" in finding.fix


def test_a_privileged_role_warns_without_blocking():
    adapter = FakeAdapter({"SELECT CURRENT_ROLE()": [("ACCOUNTADMIN",)]})
    finding = check_role(adapter)
    assert not finding.ok and not finding.fatal
    assert "SELECT-only role" in finding.fix


def test_an_ordinary_role_passes():
    adapter = FakeAdapter({"SELECT CURRENT_ROLE()": [("ASSAY_RO",)]})
    assert check_role(adapter).ok


# ---- objects ------------------------------------------------------------


def test_resolvable_contracts_pass():
    adapter = FakeAdapter({CATALOG: [("ORDERS", "ORDERED_AT"), ("ORDERS", "AMOUNT")]})
    assert check_objects(adapter, _contracts())[0].ok


def test_a_missing_table_is_blocking_and_named():
    adapter = FakeAdapter({CATALOG: [("CUSTOMERS", "ID")]})
    finding = check_objects(adapter, _contracts())[0]
    assert finding.fatal and "ORDERS" in finding.detail


def test_a_missing_column_is_named_with_its_table():
    adapter = FakeAdapter({CATALOG: [("ORDERS", "AMOUNT")]})
    finding = check_objects(adapter, _contracts())[0]
    assert "ORDERS.ORDERED_AT" in finding.detail


def test_an_empty_schema_is_blocking():
    adapter = FakeAdapter({CATALOG: []})
    finding = check_objects(adapter, _contracts())[0]
    assert finding.fatal and "no tables" in finding.detail


def test_join_keys_and_dimension_columns_are_checked():
    metric = _metric(
        dimensions=({"name": "region", "column": "NAME", "table": "REGIONS"},),
        joins=({"table": "REGIONS", "left_key": "REGION_CODE", "right_key": "CODE"},),
    )
    adapter = FakeAdapter(
        {CATALOG: [("ORDERS", "ORDERED_AT"), ("ORDERS", "REGION_CODE"), ("REGIONS", "CODE")]}
    )
    finding = check_objects(adapter, _contracts(metric))[0]
    assert "REGIONS.NAME" in finding.detail


def test_object_resolution_ignores_case():
    """The catalog reports upper case; contracts are written lower case."""
    adapter = FakeAdapter({CATALOG: [("ORDERS", "ORDERED_AT")]})
    metric = _metric(table="orders", time_column="ordered_at")
    assert check_objects(adapter, _contracts(metric))[0].ok


# ---- case policy --------------------------------------------------------


def test_upper_case_objects_match_the_upper_policy():
    adapter = FakeAdapter({CATALOG: [("ORDERS", "A"), ("REGIONS", "B")]})
    assert check_case_policy(adapter, "upper").ok


def test_lower_case_objects_under_the_upper_policy_are_blocking():
    adapter = FakeAdapter({CATALOG: [("orders", "a"), ("regions", "b")]})
    finding = check_case_policy(adapter, "upper")
    assert finding.fatal and "--case-policy exact" in finding.fix


# ---- row counts ---------------------------------------------------------


def test_row_counts_are_summed():
    adapter = FakeAdapter({"count(*)": [(1000,)], CATALOG: []})
    assert "1,000 rows" in check_row_counts(adapter, _contracts()).detail


def test_an_empty_table_warns_because_checks_would_silently_skip():
    adapter = FakeAdapter({"count(*)": [(0,)]})
    finding = check_row_counts(adapter, _contracts())
    assert not finding.ok
    assert "skip rather than fail" in finding.fix


# ---- rendering ----------------------------------------------------------


AT = datetime(2026, 9, 2, 19, 0)


def test_render_shows_a_fix_only_where_there_is_one():
    out = render(
        [Finding("a", True, "fine"), Finding("b", False, "broken", "do this")], "duckdb", AT
    )
    assert "fix: do this" in out
    assert out.count("fix:") == 1


def test_render_counts_blocking_problems_separately():
    out = render(
        [Finding("a", False, "warn"), Finding("b", False, "stop", fatal=True)], "duckdb", AT
    )
    assert "2 problem(s), 1 of them blocking" in out


def test_render_says_so_when_everything_passes():
    assert "All clear" in render([Finding("a", True, "fine")], "duckdb", AT)


@pytest.mark.parametrize(
    "finding,icon",
    [
        (Finding("a", True, ""), "✓"),
        (Finding("a", False, ""), "⚠"),
        (Finding("a", False, "", fatal=True), "✖"),
    ],
)
def test_icons_distinguish_warning_from_blocking(finding, icon):
    assert finding.icon == icon


def test_row_counts_include_joined_tables_not_only_base_tables():
    """A metric reads fine while the dimension table it joins to is empty, and
    that failure looks like a decomposition problem rather than a missing load."""
    metric = _metric(
        joins=({"table": "REGIONS", "left_key": "REGION_CODE", "right_key": "CODE"},)
    )
    counted: list[str] = []

    class Counting(FakeAdapter):
        def fetch(self, query):
            if "count(*)" in query.sql:
                counted.append(query.sql)
                return [(10,)]
            return super().fetch(query)

    finding = check_row_counts(Counting({}), _contracts(metric))
    assert len(counted) == 2
    assert "across 2 tables" in finding.detail
