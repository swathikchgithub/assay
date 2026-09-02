# Assay — P0

The invariant engine from the [Assay design spec](https://claude.ai/code/artifact/aa873390-ac8d-442c-bc47-7be08c416ca1),
phase P0. It runs against metric definitions a team already has and reports
what is wrong with numbers they already ship to executives.

**No language model. No query interface. No dashboard.** Those are P3. This
phase exists to answer one question, cheaply and falsifiably:

> Do checks generated from existing metric definitions find real defects in
> numbers a team already trusts?

If a team is indifferent to what this reports, the premise of everything
later in the spec is wrong, and three weeks of work said so.

## Run the demo

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python demo/seed.py demo/demo.duckdb
.venv/bin/python -m assay.run.cli \
  --contracts demo/contracts.yml \
  --database demo/demo.duckdb \
  --as-of 2026-09-01T09:00:00+00:00
```

Then restate a closed month and run again — this is the finding no existing
tool reports:

```bash
.venv/bin/python demo/seed.py demo/demo.duckdb --backfill
.venv/bin/python -m assay.run.cli --contracts demo/contracts.yml \
  --database demo/demo.duckdb --as-of 2026-09-02T09:00:00+00:00
```

Exit code is 1 when a block-severity check fails, so it gates a CI job as-is.

## What the demo warehouse contains

Seven defects, all of which look completely normal in a dashboard:

| # | Planted defect | Caught by |
|---|---|---|
| 1 | The `regions` lookup never got the LATAM row | `CON-01` — 10% of revenue vanishes when sliced by region |
| 2 | An integration creates accounts with no segment | `CON-02` — 5.5% of revenue unattributed |
| 3 | `order_items` is at line-item grain, not order grain | `CON-04` — 2.49x fan-out, and `CON-01` inflation by 149% |
| 4 | The spring promo batch file was loaded twice | `IDN-01` — `net ≠ gross − discounts` by 3.2% |
| 5 | `active_users` counts distinct users but is declared additive | `IDN-03` — daily rollup is 15x the true monthly figure |
| 6 | The ticket pipeline stopped 40 hours ago | `TMP-01` — against a 24h SLA |
| 7 | A late CRM sync adds orders to a closed month | `TMP-03` — April 2026 moved 191% since last night |

Defect 5 is the one worth dwelling on. Nothing is wrong with the data or the
SQL; the metric is *labelled* wrong, and every consumer who sums a daily
series is quietly told to triple-count. No schema test, freshness monitor, or
dbt test catches it.

## Warehouses

DuckDB and Snowflake. `--target snowflake` reads its connection from the
environment and nothing else:

```bash
export SNOWFLAKE_ACCOUNT=acme-eu SNOWFLAKE_USER=assay_reader \
       SNOWFLAKE_WAREHOUSE=ANALYTICS_WH SNOWFLAKE_DATABASE=PROD \
       SNOWFLAKE_SCHEMA=MART SNOWFLAKE_ROLE=ASSAY_RO
.venv/bin/python -m assay.run.cli --contracts contracts.yml --target snowflake
```

Auth is SSO (`externalbrowser`) by default, or key-pair via
`SNOWFLAKE_PRIVATE_KEY_FILE`. **`SNOWFLAKE_PASSWORD` is refused** — Assay does
not handle passwords, and a credential passed as a CLI argument lands in shell
history and in `ps` output for every other user on the host.

Three Snowflake differences are handled explicitly, because each one produces
a *wrong answer* rather than an error:

- **Identifier case.** Snowflake folds unquoted identifiers to upper case, so a
  table created as `orders` is stored as `ORDERS` and `"orders"` resolves to a
  different, usually non-existent object. Contract identifiers are folded to
  upper before quoting. A project that deliberately created quoted lower-case
  objects needs `--case-policy exact`; no single rule satisfies both, so it is
  a setting rather than a guess.
- **Bind style.** The connector defaults to `pyformat`; Assay generates `?`
  placeholders, so the adapter switches it to `qmark` at connect time.
- **No read-only connection.** DuckDB has a read-only flag and Snowflake does
  not, so the guarantee is enforced twice: connect with a SELECT-only role, and
  every statement is checked before it is sent.

Adding a third warehouse is a new `Dialect` + adapter and one branch in
`engine/targets.py` — no invariant changes.

## Design

```
contracts/   models, restricted expression evaluator, YAML + dbt sources
engine/      warehouse seam (protocol) + DuckDB adapter + SQL construction
invariants/  CON-01..04, IDN-01..03, TMP-01..04, and the registry that
             generates them from contracts
run/         orchestration, sqlite history, markdown + Slack reporting, CLI
```

Four decisions carry the design:

**Checks are generated, not written.** `invariants/registry.py` turns a
contract set into a suite. A golden-query file rots exactly as fast as the
semantic layer it tests; a generated suite does not. The registry's guards
matter as much as its generators — it will not emit `CON-01` for a
non-additive metric, because a permanent false positive trains people to
ignore the report.

**Everything crosses one seam.** Invariants talk to a `WarehouseAdapter`
protocol, never a driver, so the same suite runs against DuckDB in tests and
Snowflake in production. `MetricSQL` is the only place identifiers are quoted
and the only place values are bound.

**One scan, many checks.** `CheckContext.fetch` memoizes per run, so `CON-01`
and `CON-02` — which legitimately need the same grouped query — pay for it
once. Query amplification is the fastest way to get a verification tool
uninstalled.

**The clock is injected.** `--as-of` makes every temporal check
deterministic. Tests assert exact lag hours rather than approximating.

## What is deliberately not here

- **`CON-03` is metric-scoped.** With no planner there are no user filters, so
  it reports what a metric's own `where` removes. It becomes per-query at P3.
- **`STR-01..09`, `REC-01..02`, `DRF-01..04`, `NAR-01..04`** are out of phase.
  Structural checks need the plan algebra; reconciliation needs a configured
  external source of record; narrative checks need an answer to have prose.
- **No notification is sent by default.** `--notify` requires
  `ASSAY_SLACK_WEBHOOK`; without both, the payload is only printed. A
  verification tool that surprises a channel on first run gets muted before it
  says anything useful.

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

114 tests. The unit suite runs against a fake adapter with canned rows, so each
invariant is exercised in isolation and in milliseconds. The integration suite
seeds a real DuckDB warehouse with the seven planted defects and asserts each
one is found and named, then restates a closed month and asserts the second
run notices — including that metrics the backfill did not touch stay quiet.

The Snowflake adapter is tested against a fake connector rather than a live
account, so identifier folding, bind style, the read-only guard, and credential
handling are all covered offline. What that cannot cover is whether a real
project's objects resolve under the default case policy — that is the first
thing to check when pointing it at a live warehouse.
