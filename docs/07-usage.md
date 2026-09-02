# 7 · Usage

## Install

```bash
uv venv --python 3.12 .venv
```

```bash
uv pip install --python .venv/bin/python -e ".[dev]"
```

Snowflake needs an extra:

```bash
uv pip install --python .venv/bin/python -e ".[snowflake]"
```

> `uv venv` deliberately does not install `pip` into the environment, so
> `uv pip install --python .venv/bin/python ...` is the idiom here even with
> the venv activated.

## Try it with no warehouse

```bash
.venv/bin/python -m demo.seed demo/demo.duckdb
```

```bash
.venv/bin/python -m assay.run.cli --contracts demo/contracts.yml --database demo/demo.duckdb --as-of 2026-09-01T09:00:00+00:00
```

Five failures and two warnings. Then restate a closed month and run again —
this is the finding no other tool reports:

```bash
.venv/bin/python -m demo.seed demo/demo.duckdb --backfill
```

```bash
.venv/bin/python -m assay.run.cli --contracts demo/contracts.yml --database demo/demo.duckdb --as-of 2026-09-02T09:00:00+00:00
```

## Writing contracts

Import from dbt where possible — `--dbt-manifest target/semantic_manifest.json`
— and hand-write only what dbt cannot express.

```yaml
version: my-project-v1

metrics:
  - name: net_revenue
    table: orders
    measure: sum(amount - discount)
    time_column: ordered_at
    additivity: additive          # the field that decides which checks exist
    tolerance: 0.001              # relative
    unit: money
    freshness_sla_hours: 48
    owner: finance
    joins:
      - {table: regions,  left_key: region_code, right_key: code, kind: inner}
      - {table: accounts, left_key: account_id,  right_key: id,   kind: left}
    dimensions:
      - {name: region,  column: name,    table: regions}
      - {name: segment, column: segment, table: accounts}

  - name: gross_revenue
    table: orders
    measure: sum(amount)
    time_column: ordered_at
    additivity: additive

identities:
  - name: net_is_gross_less_discounts
    lhs: net_revenue
    rhs: gross_revenue - discount_total
    tolerance: 0.002
```

### Fields that change what gets checked

| Field | Effect |
|---|---|
| `additivity` | `additive` generates CON-01/02 and IDN-03; `non_additive` generates neither |
| `dimensions` | One CON-01 and one CON-02 per dimension |
| `joins` | One CON-04 per join not marked `required` |
| `where` | Generates CON-03 |
| `freshness_sla_hours` | Generates TMP-01 |
| `derived` | Generates IDN-02 |
| `tolerance` | Relative threshold for CON-01, IDN-01/03, TMP-03 |

**Declare additivity honestly.** Declaring a distinct count `additive` is the
defect IDN-03 exists to find — it will be reported, correctly.

**Mark a join `required: true`** only when the measure or filter genuinely
depends on it. Required joins appear in the ungrouped total and are not
fan-out-checked.

## Running

### DuckDB

```bash
.venv/bin/python -m assay.run.cli --contracts contracts.yml --database warehouse.duckdb
```

### Snowflake

Connection comes from the environment only — never a CLI flag, which lands in
shell history and `ps` output.

```bash
export SNOWFLAKE_ACCOUNT=ORG-ACCOUNT SNOWFLAKE_USER=assay_reader SNOWFLAKE_WAREHOUSE=WH SNOWFLAKE_DATABASE=ANALYTICS SNOWFLAKE_SCHEMA=MART SNOWFLAKE_ROLE=ASSAY_RO SNOWFLAKE_PRIVATE_KEY_FILE=$HOME/.snowflake/assay_key.p8
```

```bash
.venv/bin/python -m assay.run.cli --dbt-manifest target/semantic_manifest.json --target snowflake
```

`SNOWFLAKE_PASSWORD` is refused. Use key-pair auth or SSO — see
[operations](08-operations.md#authentication).

### Options

| Flag | Default | Notes |
|---|---|---|
| `--contracts` / `--dbt-manifest` | required | Mutually exclusive |
| `--target` | `duckdb` | `duckdb` or `snowflake` |
| `--database` | — | DuckDB file; required for that target |
| `--history` | `.assay/history.db` | Baseline for restatement detection |
| `--since-days` | `540` | Lookback window |
| `--as-of` | now | ISO timestamp; makes a run reproducible |
| `--case-policy` | `upper` | Snowflake identifier folding |
| `--format` | `markdown` | `markdown` or `json` |
| `--verbose` | off | Include passing checks |
| `--notify` | off | Post to `ASSAY_SLACK_WEBHOOK` |

## Diagnosing setup

Before the first run against a new warehouse:

```bash
.venv/bin/python -m assay.run.doctor_cli --contracts contracts.yml --target snowflake
```

Checks configuration, endpoint certificate, connection, session, role, case
policy, every object the contracts reference, and row counts. Exit `0` clean,
`1` warnings, `2` blocking.

## In CI

`assay run` exits `2` on a blocking failure, so it gates unchanged:

```yaml
- name: Check metrics
  env:
    SNOWFLAKE_ACCOUNT: ${{ secrets.SNOWFLAKE_ACCOUNT }}
    SNOWFLAKE_USER: ${{ secrets.SNOWFLAKE_USER }}
    SNOWFLAKE_PRIVATE_KEY_FILE: /tmp/assay_key.p8
    SNOWFLAKE_WAREHOUSE: ANALYTICS_WH
    SNOWFLAKE_DATABASE: ANALYTICS
    SNOWFLAKE_SCHEMA: MART
    SNOWFLAKE_ROLE: ASSAY_RO
  run: |
    python -m assay.run.doctor_cli --dbt-manifest target/semantic_manifest.json --target snowflake
    python -m assay.run.cli --dbt-manifest target/semantic_manifest.json --target snowflake
```

**Restatement detection needs history to persist between runs.** Cache or
otherwise retain `.assay/history.db`, or TMP-03 records a baseline and skips
forever.

## Slack

```bash
export ASSAY_SLACK_WEBHOOK=https://hooks.slack.com/services/...
```

```bash
.venv/bin/python -m assay.run.cli --contracts contracts.yml --database warehouse.duckdb --notify
```

Nothing is sent without both the webhook and `--notify`. A verification tool
that messages a channel by surprise on first run gets muted before it has said
anything useful.
