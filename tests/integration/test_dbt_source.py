"""Importing contracts from a dbt project — P0's actual on-ramp."""

import json

import pytest

from assay.contracts.models import Additivity
from assay.contracts.sources import DbtManifestSource

MANIFEST = {
    "semantic_models": [
        {
            "name": "orders",
            "node_relation": {"alias": "orders"},
            "owners": ["finance"],
            "dimensions": [
                {"name": "ordered_at", "type": "time"},
                {"name": "region", "type": "categorical"},
            ],
            "measures": [
                {"name": "revenue", "agg": "sum", "expr": "amount"},
                {"name": "buyers", "agg": "count_distinct", "expr": "account_id"},
            ],
        }
    ]
}


@pytest.fixture
def manifest(tmp_path):
    path = tmp_path / "semantic_manifest.json"
    path.write_text(json.dumps(MANIFEST))
    return path


def test_measures_become_metrics(manifest):
    contracts = DbtManifestSource(manifest).load()
    assert {m.name for m in contracts.metrics} == {"revenue", "buyers"}


def test_aggregation_becomes_a_sql_measure(manifest):
    assert DbtManifestSource(manifest).load().metric("revenue").measure == "sum(amount)"


def test_a_distinct_count_is_imported_as_non_additive(manifest):
    """dbt does not record additivity, so it is inferred — and this is the
    inference that makes IDN-03 meaningful on an untouched project."""
    metric = DbtManifestSource(manifest).load().metric("buyers")
    assert metric.additivity is Additivity.NON_ADDITIVE


def test_the_time_dimension_becomes_the_time_column(manifest):
    assert DbtManifestSource(manifest).load().metric("revenue").time_column == "ordered_at"


def test_categorical_dimensions_are_carried_over(manifest):
    metric = DbtManifestSource(manifest).load().metric("revenue")
    assert [d.name for d in metric.dimensions] == ["region"]


def test_a_model_without_a_time_dimension_is_rejected(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"semantic_models": [
        {"name": "x", "node_relation": {"alias": "x"}, "dimensions": [],
         "measures": [{"name": "n", "agg": "sum", "expr": "a"}]}
    ]}))
    with pytest.raises(ValueError, match="declares no time dimension"):
        DbtManifestSource(path).load()
