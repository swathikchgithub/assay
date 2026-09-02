"""Generation guards.

The registry decides which checks a contract implies. Generating the wrong
one is worse than generating none: a permanent false positive teaches people
to ignore the report.
"""

from assay.contracts.models import ContractSet, Identity, Metric
from assay.engine.duckdb_adapter import DuckDBDialect
from assay.invariants.registry import generate


def _ids(contracts: ContractSet) -> list[str]:
    return [inv.id for inv in generate(contracts, DuckDBDialect())]


def _metric(**overrides) -> Metric:
    base = {
        "name": "revenue",
        "table": "orders",
        "measure": "sum(amount)",
        "time_column": "ts",
        "additivity": "additive",
        "dimensions": ({"name": "region", "column": "region"},),
    }
    return Metric(**{**base, **overrides})


def test_decomposition_is_generated_for_an_additive_metric():
    assert "CON-01" in _ids(ContractSet(metrics=(_metric(),)))


def test_decomposition_is_not_generated_for_a_non_additive_metric():
    """Summing groups of a distinct count would fail forever and mean nothing."""
    metric = _metric(measure="count(distinct id)", additivity="non_additive")
    assert "CON-01" not in _ids(ContractSet(metrics=(metric,)))


def test_cross_grain_is_not_generated_where_no_rollup_rule_exists():
    metric = _metric(additivity="non_additive")
    assert "IDN-03" not in _ids(ContractSet(metrics=(metric,)))


def test_filter_mass_is_generated_only_for_a_filtered_metric():
    assert "CON-03" not in _ids(ContractSet(metrics=(_metric(),)))
    filtered = _metric(where="status <> 'cancelled'")
    assert "CON-03" in _ids(ContractSet(metrics=(filtered,)))


def test_fan_out_is_checked_only_for_optional_traversals():
    """A join the measure itself needs is in the reference total already."""
    required = _metric(
        joins=({"table": "accounts", "left_key": "a", "right_key": "b", "required": True},)
    )
    assert "CON-04" not in _ids(ContractSet(metrics=(required,)))
    optional = _metric(
        joins=({"table": "accounts", "left_key": "a", "right_key": "b"},)
    )
    assert "CON-04" in _ids(ContractSet(metrics=(optional,)))


def test_freshness_is_generated_only_where_an_sla_is_declared():
    assert "TMP-01" not in _ids(ContractSet(metrics=(_metric(),)))
    assert "TMP-01" in _ids(ContractSet(metrics=(_metric(freshness_sla_hours=24),)))


def test_declared_identities_produce_one_check_each():
    contracts = ContractSet(
        metrics=(_metric(), _metric(name="gross", dimensions=())),
        identities=(Identity(name="i", lhs="revenue", rhs="gross"),),
    )
    assert _ids(contracts).count("IDN-01") == 1


def test_a_derived_metric_generates_its_own_identity():
    contracts = ContractSet(
        metrics=(
            _metric(name="arpu", derived="revenue", dimensions=()),
            _metric(),
        )
    )
    assert "IDN-02" in _ids(contracts)
