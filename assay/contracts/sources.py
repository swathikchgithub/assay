"""Contract sources. A source turns some existing artifact into a ContractSet.

`ContractSource` is the seam that keeps the rest of the system independent of
where definitions come from (Dependency Inversion): P0 ships a YAML source and
a dbt-manifest source; a Cube or Cortex source is a new class, not an edit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

import yaml

from assay.contracts.models import ContractSet, Identity, Metric


class ContractSource(Protocol):
    """Anything that can produce a ContractSet."""

    def load(self) -> ContractSet: ...


class YamlSource:
    """Plain YAML contracts — the format the miner will eventually emit."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def load(self) -> ContractSet:
        raw = yaml.safe_load(self._path.read_text()) or {}
        return ContractSet(
            metrics=tuple(Metric(**m) for m in raw.get("metrics", [])),
            identities=tuple(Identity(**i) for i in raw.get("identities", [])),
            version=str(raw.get("version", self._path.stem)),
        )


class DbtManifestSource:
    """Import contracts from a dbt semantic manifest.

    P0 reads dbt's `semantic_manifest.json` because that is where a customer's
    definitions already live — the whole point of phase P0 is that it runs
    against an existing project with nothing new authored.
    """

    _AGGREGATION_SQL = {
        "sum": "sum({expr})",
        "count": "count({expr})",
        "count_distinct": "count(distinct {expr})",
        "average": "avg({expr})",
        "min": "min({expr})",
        "max": "max({expr})",
    }

    _ADDITIVITY = {
        "sum": "additive",
        "count": "additive",
        "average": "non_additive",
        "count_distinct": "non_additive",
        "min": "semi_additive",
        "max": "semi_additive",
    }

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def load(self) -> ContractSet:
        raw = json.loads(self._path.read_text())
        metrics = [
            self._metric(model, measure)
            for model in raw.get("semantic_models", [])
            for measure in model.get("measures", [])
        ]
        return ContractSet(metrics=tuple(metrics), version=raw.get("version", "dbt"))

    def _metric(self, model: dict[str, Any], measure: dict[str, Any]) -> Metric:
        agg = measure.get("agg", "sum")
        return Metric(
            name=measure["name"],
            table=model["node_relation"]["alias"],
            measure=self._AGGREGATION_SQL[agg].format(expr=measure["expr"]),
            time_column=self._time_column(model),
            additivity=self._ADDITIVITY[agg],
            dimensions=tuple(self._dimensions(model)),
            owner=model.get("owners", [None])[0],
        )

    @staticmethod
    def _time_column(model: dict[str, Any]) -> str:
        for dim in model.get("dimensions", []):
            if dim.get("type") == "time":
                return dim.get("expr") or dim["name"]
        raise ValueError(f"semantic model {model['name']!r} declares no time dimension")

    @staticmethod
    def _dimensions(model: dict[str, Any]) -> list[dict[str, str]]:
        return [
            {"name": d["name"], "column": d.get("expr") or d["name"]}
            for d in model.get("dimensions", [])
            if d.get("type") == "categorical"
        ]
