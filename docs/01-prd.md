# 1 · Product requirements

## The problem

Analytics organisations have spent a decade solving *consistency* — semantic
layers, metric stores, governed definitions, a single source of truth. Those
solve disagreement: two teams reporting different revenue numbers.

They do not solve *wrongness*. A governed metric computed identically
everywhere is still wrong everywhere if the definition, the join, or the grain
is wrong. Governance makes an error consistent; it does not make it visible.

The failure has a shape specific to this domain:

> **The person consuming a number is the person least equipped to check it.**

In every other software domain a defect announces itself — wrong code fails to
compile, a broken layout looks broken. A wrong number looks exactly like a
right number. So trust does not degrade gracefully; it collapses permanently
the first time a bad figure reaches a board meeting, and no amount of
subsequent correctness rebuilds it.

## Who it is for

**Primary user — the analytics or data platform engineer.** Owns dbt models
and metric definitions. Finds out numbers were wrong when someone senior asks.
Wants to know first.

**Beneficiary — whoever consumes the numbers.** Never runs Assay, never sees
its output directly. Benefits because the numbers reaching them were attacked
before they arrived.

**Not the user — the executive asking the question.** P0 has no interface for
them by design. See [decision D-07](04-design-decisions.md#d-07--the-planner-ships-last).

## Goals

| | Goal | Measured by |
|---|---|---|
| G1 | Find real defects in metrics a team already trusts | Defects found per project on first run |
| G2 | Require no new authoring to start | Time from install to first finding |
| G3 | Produce findings a human acts on, not an alert stream | Ratio of findings acted on to findings ignored |
| G4 | Cost less than the trust it protects | Warehouse credits per run |
| G5 | Never be the reason data changed | Zero write statements issued, structurally |

## Non-goals

- **Not a BI tool.** No dashboards, no visual exploration.
- **Not a query engine.** Compiles to whatever the customer already bought.
- **Not a data catalogue.** Definitions exist here to be executed and tested,
  not browsed.
- **Not a column-level data quality tool.** `not_null` and `unique` are dbt's
  job and dbt does them well. Assay operates one layer up, on metrics.
- **No natural-language interface in P0.** Deliberately scheduled last.

## Requirements

### Functional

- **R1** Import metric definitions from artefacts that already exist (a dbt
  semantic manifest), and from plain YAML for teams without dbt.
- **R2** Generate checks from those definitions. No per-customer check
  authoring.
- **R3** Detect defects in four families: conservation (arithmetic does not
  close), identity (metrics contradict each other), temporal (the past
  changed, data is stale), and structural (illegal by definition).
- **R4** Report findings in language naming the metric, the figure, and the
  consequence — not a check id and a boolean.
- **R5** Exit non-zero on a blocking failure so it gates CI unchanged.
- **R6** Run against more than one warehouse without changing the checks.
- **R7** Diagnose its own misconfiguration before it costs anyone an evening.

### Non-functional

- **N1 Read-only.** Structurally incapable of writing to the warehouse. A
  verification layer that can write is one that can corrupt what it verifies.
- **N2 Deterministic.** Same inputs, same findings. The clock is injected.
- **N3 Cheap.** Scans, not rows, are the cost driver; repeated queries within
  a run are paid for once.
- **N4 Least privilege.** Runs under a `SELECT`-only role.
- **N5 No secrets in arguments.** Credentials from the environment only; never
  a CLI flag, which lands in shell history and `ps` output.

## Success criteria for P0

P0 exists to falsify one hypothesis as cheaply as possible:

> Do checks generated from existing metric definitions find defects a team
> cares about in numbers they already ship?

**Achieved.** Eleven generated checks find all seven planted defects, on both
DuckDB and a live Snowflake account, byte-identically, under a read-only role.

**Not yet achieved.** Every defect so far was planted by us. The open question
— the one that decides whether the product exists — is whether these checks
find anything a team *cares about* in their own numbers. That needs
`--dbt-manifest` pointed at a real project. See [roadmap](09-roadmap.md).

## What would falsify the premise

If a team is shown a genuine defect in a number they ship to executives, and
shrugs, then the premise of everything after P0 is wrong. That is the cheapest
possible disproof and it is why P0 carries no language model, no interface, and
no new query surface — three weeks, not nine months, to find out.
