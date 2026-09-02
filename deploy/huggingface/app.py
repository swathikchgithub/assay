"""Assay playground — every control edits the contract, never the data.

The warehouse is seeded once and never changes. What changes is what the
metric definitions *claim* about it, and therefore which checks exist at all.
Flip `active_users` from non-additive to additive and a 1477% discrepancy
appears out of a single label. That is the whole argument, made touchable.
"""

from __future__ import annotations

import tempfile
from datetime import timedelta, timezone
from pathlib import Path

import gradio as gr

from assay.contracts.models import ContractSet, Join, JoinType, Metric
from assay.contracts.sources import YamlSource
from assay.engine.duckdb_adapter import DuckDBAdapter
from assay.engine.sql import Window
from assay.invariants.base import Status
from assay.run.history import History
from assay.run.report import markdown
from assay.run.runner import run
from demo import data, seed

ROOT = Path(__file__).resolve().parent
WORK = Path(tempfile.gettempdir()) / "assay-playground"
WAREHOUSE = WORK / "demo.duckdb"
HISTORY = WORK / "history.db"
CONTRACTS = ROOT / "demo" / "contracts.yml"
DAYS = 365
LOOKBACK = 400

_cycles = {"n": 0}


def _ensure_warehouse() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    if not WAREHOUSE.exists():
        seed.seed(WAREHOUSE, days=DAYS)


def _variant(
    additive_users: bool,
    region_join: str,
    include_sku: bool,
    check_identity: bool,
    freshness_hours: int,
) -> ContractSet:
    """The same warehouse, described by different contracts."""
    base = YamlSource(CONTRACTS).load()
    metrics = []
    for metric in base.metrics:
        if metric.name == "active_users":
            metric = metric.model_copy(
                update={"additivity": "additive" if additive_users else "non_additive"}
            )
        if metric.name == "open_tickets":
            metric = metric.model_copy(update={"freshness_sla_hours": freshness_hours})
        if metric.name == "net_revenue":
            metric = _shape_revenue(metric, region_join, include_sku)
        metrics.append(metric)
    identities = base.identities if check_identity else ()
    return ContractSet(metrics=tuple(metrics), identities=identities)


def _shape_revenue(metric: Metric, region_join: str, include_sku: bool) -> Metric:
    joins = tuple(
        Join(
            table=j.table,
            left_key=j.left_key,
            right_key=j.right_key,
            kind=JoinType(region_join) if j.table == "regions" else j.kind,
            required=j.required,
        )
        for j in metric.joins
        if include_sku or j.table != "order_items"
    )
    dimensions = tuple(
        d for d in metric.dimensions if include_sku or d.table != "order_items"
    )
    return metric.model_copy(update={"joins": joins, "dimensions": dimensions})


def check(additive_users, region_join, include_sku, check_identity, freshness_hours):
    _ensure_warehouse()
    as_of = data.AS_OF.replace(tzinfo=timezone.utc) + timedelta(days=_cycles["n"])
    history = History(HISTORY)
    with DuckDBAdapter(str(WAREHOUSE), as_of=as_of) as adapter:
        summary = run(
            _variant(additive_users, region_join, include_sku, check_identity, int(freshness_hours)),
            adapter,
            Window(start=(as_of - timedelta(days=LOOKBACK)).date()),
            history=history,
        )
    history.record_checks(summary.run_id, summary.results, summary.ran_at)
    history.close()
    return markdown(summary, include_passing=True), _headline(summary)


def _headline(summary) -> str:
    return (
        f"**{len(summary.failures)} failed · {len(summary.warnings)} warned · "
        f"{len(summary.by_status(Status.PASS))} passed** — "
        f"{summary.scans} warehouse scans in {summary.duration_s:.2f}s"
    )


def restate(additive_users, region_join, include_sku, check_identity, freshness_hours):
    """Rewrite a closed month, then check again. This is TMP-03."""
    _ensure_warehouse()
    seed.seed(WAREHOUSE, backfill=True)
    _cycles["n"] += 1
    return check(additive_users, region_join, include_sku, check_identity, freshness_hours)


INTRO = """
# Assay playground

A synthetic warehouse with seven planted defects, and six metric definitions
describing it. **Every control below edits a definition, never the data.**

That is the point. The checks are *generated from the contract*, so changing
what a metric claims about itself changes which checks exist. Flip
`active_users` to additive and a 1477% discrepancy appears from a single
label — no schema test, freshness monitor, or dbt test would see it.

Run it once, then press **Restate a closed month** to reproduce the finding
almost nothing else reports: a month everyone considered finished, silently
rewritten.
"""


with gr.Blocks(title="Assay playground") as demo:
    gr.Markdown(INTRO)
    with gr.Row():
        with gr.Column(scale=1):
            gr.Markdown("### The contract")
            additive_users = gr.Checkbox(
                label="active_users declared additive",
                value=True,
                info="It is a distinct count. Declaring it additive is a lie → IDN-03",
            )
            region_join = gr.Radio(
                ["inner", "left"],
                value="inner",
                label="net_revenue → regions join",
                info="The lookup table is missing a region. Inner drops those rows → CON-01",
            )
            include_sku = gr.Checkbox(
                label="slice revenue by sku",
                value=True,
                info="order_items is line-item grain, not order grain → CON-04 fan-out",
            )
            check_identity = gr.Checkbox(
                label="assert net = gross − discounts",
                value=True,
                info="A promo batch was loaded twice → IDN-01",
            )
            freshness_hours = gr.Slider(
                12, 96, value=24, step=12,
                label="open_tickets freshness SLA (hours)",
                info="The pipeline stopped 40h ago → TMP-01 above 40",
            )
            run_btn = gr.Button("Run Assay", variant="primary")
            restate_btn = gr.Button("Restate a closed month, then run")
        with gr.Column(scale=2):
            headline = gr.Markdown()
            report = gr.Markdown()

    inputs = [additive_users, region_join, include_sku, check_identity, freshness_hours]
    run_btn.click(check, inputs=inputs, outputs=[report, headline])
    restate_btn.click(restate, inputs=inputs, outputs=[report, headline])
    demo.load(check, inputs=inputs, outputs=[report, headline])

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)
