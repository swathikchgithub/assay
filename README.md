# Assay — P0

Assay runs against the metric definitions a team already has and reports what
is wrong with the numbers they already ship to executives.

A governed metric guarantees *consistency*, not *correctness* — every dashboard
showing the same wrong number consistently is exactly what governance buys you.
Assay checks the numbers themselves, continuously, and generates the checks
from the definitions rather than asking anyone to write them.

**No language model. No query interface. No dashboard.** Those are P3. This
phase exists to answer one question, cheaply and falsifiably:

> Do checks generated from existing metric definitions find real defects in
> numbers a team already trusts?

If a team is indifferent to what this reports, the premise of everything
later in the spec is wrong, and three weeks of work said so.

## Documentation

Full docs in [`docs/`](docs/):

| | | |
|---|---|---|
| [Architecture spec (P0-P3)](docs/00-architecture-spec.md) | [How it works](docs/02-how-it-works.md) | [Technical design](docs/03-tdd.md) |
| [Design decisions & alternatives](docs/04-design-decisions.md) | [Invariant reference](docs/05-invariants.md) | [Code walkthrough](docs/06-code-walkthrough.md) |
| [Usage](docs/07-usage.md) | [Operations](docs/08-operations.md) | [Roadmap](docs/09-roadmap.md) |
| [Deployment & showcase](docs/10-deployment.md) | | |

If you read one, read [design decisions](docs/04-design-decisions.md) — it
covers why this is not a dbt test, not an anomaly detector, and not a golden
query suite.

## What it looks like

```
2026-09-01 09:00 UTC · 5 failed, 2 warned, 24 passed, 6 skipped · 24 scans · 0.05s

## Failed (5)

- ✖ CON-01  net_revenue by region  — grouping by region accounts for 17,966,839.32
           of 20,063,846.55 — 2,097,007.23 (10.45%) disappears in the traversal
           to regions
- ✖ CON-04  net_revenue -> order_items — fan-out: joining order_items turns 16,110
           rows into 40,341 (2.50x) — every additive measure over this path is
           overstated
- ✖ IDN-03  active_users — 2026-01: daily values rolled up give 14,193.00, computed
           natively at month gives 900.00 (1477.00% apart) — `active_users` is
           declared additive but does not roll up that way

## Warnings (2)

- ⚠ CON-02  net_revenue by segment — 5.19% of net_revenue has no segment
- ⚠ TMP-01  open_tickets — newest row is 40.0h old against a 24h SLA
```

Every one of those is a defect an analytics team actually ships, and none of
them looks wrong in a dashboard. `IDN-03` is the one worth dwelling on: nothing
is wrong with the data or the SQL — the metric is *labelled* wrong, so every
consumer who sums a daily series is quietly told to triple-count. No schema
test, freshness monitor, or dbt test catches it.

## Run the demo

No warehouse, no credentials, no account — it runs against a local DuckDB file
in about a minute.

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m demo.seed demo/demo.duckdb
.venv/bin/python -m assay.run.cli \
  --contracts demo/contracts.yml \
  --database demo/demo.duckdb \
  --as-of 2026-09-01T09:00:00+00:00
```

Then restate a closed month and run again — this is the finding no existing
tool reports:

```bash
.venv/bin/python -m demo.seed demo/demo.duckdb --backfill
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

## Diagnosing a setup

```bash
.venv/bin/python -m assay.run.doctor_cli --contracts contracts.yml --target snowflake
```

```
Assay doctor · target=snowflake · 2026-09-02 19:38 UTC

  ✓  configuration      account ACME-PROD as assay_reader · key-pair auth
  ✓  endpoint           acme-prod.snowflakecomputing.com certificate valid
  ✓  connection         snowflake reachable
  ✓  session            ASSAY_RO @ COMPUTE_WH · ASSAY_DEMO.DEMO
  ✓  role               ASSAY_RO is not a privileged built-in role
  ✓  case policy        objects are UPPER CASE; --case-policy upper is correct
  ✓  contract objects   6 metrics resolved against 7 tables, 22 columns
  ✓  row counts         329,457 rows across 7 tables
```

Exit `0` clean, `1` warnings only, `2` something blocking.

Every check here exists because bringing this up against a real account failed
in a way whose error named the symptom and not the cause:

- **A wrong account identifier** surfaced as a TLS hostname mismatch forty
  stack frames deep. Snowflake wildcards its DNS, so a bad identifier resolves
  happily and lands you on someone else's deployment — only the certificate
  reveals it. `check_endpoint` verifies the certificate before authenticating,
  and names `SYSTEM$ALLOWLIST()` as the way to get the right value.
- **A missing SAML provider** surfaced as `390190`. Configuration is checked
  first, so a missing variable never reaches the point of opening a browser.
- **Empty tables** would surface as a report where every check skipped, which
  reads like success. `check_row_counts` says so instead.
- **A wrong case policy** would surface as every object appearing not to exist.
  Object resolution is case-insensitive and the policy is reported separately,
  so the two failures do not look alike.

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

### Proving the adapter against a real warehouse

The fake-connector tests cover everything except whether real Snowflake
behaves as assumed. To settle that, load the same demo dataset into Snowflake
and run the identical contracts against it — same seven defects, same expected
findings, so any divergence is the adapter's fault and nowhere else.

```sql
CREATE DATABASE IF NOT EXISTS ASSAY_DEMO;
```

```bash
.venv/bin/python -m demo.load_snowflake --database ASSAY_DEMO --schema DEMO
```

That prints a plan and writes nothing. Add `--yes` to execute. Then point the
run at Snowflake with `SNOWFLAKE_DATABASE=ASSAY_DEMO SNOWFLAKE_SCHEMA=DEMO` —
the contracts use bare table names, so they resolve against the connection's
database and schema and need no change.

**Verified.** Run against a live Snowflake account (AWS us-east-2, key-pair
auth, XS warehouse), the report is byte-for-byte identical to the DuckDB one —
same five failures, same two warnings, same figures to the cent. Every offline
assumption held: upper-case identifier folding resolved the tables, `qmark`
binding worked, `DATE_TRUNC` agreed. The `events` table is generated by
*different SQL* on each warehouse (`generate_series` vs `GENERATOR`/`SEQ4`) and
still produced identical distinct-user counts, which is what validates aliasing
`SEQ4()` once in a subquery.

The restatement loop is verified too: backfilling a closed month in Snowflake
and re-running reports April 2026 moving on exactly the three revenue metrics,
identical to DuckDB, and stays quiet on the two the backfill did not touch. All
seven planted defects are therefore found on a real warehouse, not just
locally.

The timing is the interesting part: 8.95s against Snowflake versus 0.05s
against DuckDB, for the same 24 scans. Roughly a third of a second per scan is
round-trip latency, so on a real warehouse the scan count — not the row count —
is what the nightly job costs. `CheckContext.fetch` memoizing 31 checks down to
24 scans stops being a nicety there.

`demo/load_snowflake.py` is the only code in the repo that writes. It talks to
the connector directly rather than through `SnowflakeAdapter`, which refuses
non-queries, and it lives in `demo/` well away from anything the nightly run
imports. It drops and recreates seven tables in a schema you name, which is
why it plans first and needs `--yes`.

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

## Layout

```
assay/contracts/   models, restricted expression evaluator, YAML + dbt sources
assay/engine/      warehouse seam, DuckDB + Snowflake adapters, SQL builders
assay/invariants/  the check classes and the registry that generates them
assay/run/         orchestration, sqlite history, reporting, CLI
demo/data.py       the dataset and its seven defects, sink-independent
demo/seed.py       writes it to DuckDB
demo/load_snowflake.py  writes it to Snowflake (the only writing code)
```

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

159 tests. The unit suite runs against a fake adapter with canned rows, so each
invariant is exercised in isolation and in milliseconds. The integration suite
seeds a real DuckDB warehouse with the seven planted defects and asserts each
one is found and named, then restates a closed month and asserts the second
run notices — including that metrics the backfill did not touch stay quiet.

The Snowflake adapter is tested against a fake connector rather than a live
account, so identifier folding, bind style, the read-only guard, and credential
handling are all covered offline. What that cannot cover is whether a real
project's objects resolve under the default case policy — that is the first
thing to check when pointing it at a live warehouse.

## Licence

MIT — see [LICENSE](LICENSE).
