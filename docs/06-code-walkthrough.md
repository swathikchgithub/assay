# 6 · Code walkthrough

3,000 lines across 25 modules, none over 261 lines. This follows one run
through them in the order execution actually visits.

## Map

```
assay/
  contracts/     what a metric is
    models.py        168  Metric, Dimension, Join, Identity, ContractSet
    expr.py           92  restricted AST evaluator for identity expressions
    sources.py       103  ContractSource protocol · YAML · dbt manifest
  engine/        how to ask the warehouse
    adapter.py        49  WarehouseAdapter + Dialect protocols, Query
    sql.py           143  MetricSQL — the only place SQL is built
    duckdb_adapter.py 58
    snowflake_adapter.py 178
    targets.py        37  which adapter to open
  invariants/    what must be true
    base.py          107  Invariant protocol, CheckResult, CheckContext
    conservation.py  160  CON-01..04
    identity.py      157  IDN-01..03
    temporal.py      224  TMP-01..04
    registry.py      108  contracts -> check suite
    stats.py          36  median absolute deviation
  run/           orchestration and output
    runner.py        100  generate, execute, summarise
    history.py       102  SQLite snapshots and check log
    report.py        127  markdown and Slack Block Kit
    cli.py           120  assay run
    doctor.py        261  setup diagnosis
    doctor_cli.py    115  assay doctor
demo/
  data.py          192  the dataset and its seven defects, sink-independent
  seed.py           66  writes it to DuckDB
  load_snowflake.py 146  writes it to Snowflake — the only writing code
```

---

## Step 1 — Load the contracts

`contracts/sources.py` · `contracts/models.py`

```python
contracts = YamlSource(Path("contracts.yml")).load()
# or
contracts = DbtManifestSource(Path("target/semantic_manifest.json")).load()
```

`ContractSource` is a `Protocol` with one method. A new definition format is a
new class, not an edit to anything.

`DbtManifestSource` maps each measure in a semantic model to a `Metric`,
inferring additivity from aggregation type — `count_distinct` → non-additive,
`sum` → additive. **That inference is what makes IDN-03 meaningful on an
untouched project, and it is the most likely thing to be wrong.**

Validation happens at construction. `Metric` is `frozen=True`; identifiers must
match a bare-identifier pattern, and SQL fragments are rejected if they contain
`;`, `--`, `/*`, `UNION`, `DROP`, or `INSERT`. A bad contract fails at load,
not mid-run.

## Step 2 — Generate the suite

`invariants/registry.py`

```python
invariants = generate(contracts, adapter.dialect, thresholds)
```

Each metric implies its own checks. The guards are the interesting part:

```python
if metric.additivity is Additivity.ADDITIVE:
    # Summing groups is only meaningful for a metric that may be summed.
    for dim in metric.dimensions:
        out.append(DecompositionSum(metric, dim, sql))
        out.append(NullMass(metric, dim, sql, thresholds.null_mass))
```

Generating CON-01 for a distinct count would fail forever and mean nothing.
Adding a check family means adding a generator here, never editing the runner.

## Step 3 — Build the SQL

`engine/sql.py`

`MetricSQL` is the only place identifiers are quoted and values are bound. Six
builders, one per question the checks ask.

The important pair:

```python
def total(self, window):     # only joins the measure itself depends on
def grouped(self, dim, window):  # + the traversal needed to reach the dimension
```

They deliberately build different `FROM` clauses. If they shared one, their
sums would agree by construction and CON-01 would be tautological. **The
difference between them is the check.**

Window bounds are bound parameters, never interpolated:

```sql
SELECT sum(amount - discount) FROM "orders" AS b
WHERE b."ordered_at" >= ? AND b."ordered_at" < ?
```

## Step 4 — Open the warehouse

`engine/targets.py` · `engine/duckdb_adapter.py` · `engine/snowflake_adapter.py`

`open_adapter()` is the one place that knows drivers exist; the Snowflake
connector is imported lazily so the DuckDB path does not pay for it.

`SnowflakeAdapter` handles three things that produce a *wrong answer* rather
than an error:

```python
def quote(self, identifier):
    folded = identifier.upper() if self._policy == "upper" else identifier
    return '"' + folded.replace('"', '""') + '"'
```

Snowflake folds unquoted identifiers to upper case, so a table created as
`orders` is stored as `ORDERS` and emitting `"orders"` resolves to a different,
usually non-existent object.

```python
self._connector.paramstyle = "qmark"   # connector default is pyformat
```

```python
def _assert_read_only(sql):
    if not _SELECT_ONLY.match(sql):
        raise PermissionError(...)
```

Snowflake has no read-only connection flag, so the guarantee is enforced twice:
a `SELECT`-only role, and this guard.

## Step 5 — Run the checks

`run/runner.py` · `invariants/base.py`

```python
ctx = CheckContext(adapter=adapter, window=window, now=ran_at, history=history)
results = tuple(_safely(inv, ctx) for inv in invariants)
```

Two properties live here. **A broken check is reported, never fatal** —
`_safely` turns an exception into a `CheckResult`, so one bad contract cannot
stop the suite. And `CheckContext.fetch` memoises on `(sql, params)`:

```python
def fetch(self, query):
    key = (query.sql, query.params)
    if key not in self._cache:
        self._cache[key] = self.adapter.fetch(query)
        self.scans += 1
    return self._cache[key]
```

CON-01 and CON-02 legitimately need the same grouped query. Against Snowflake
a scan costs ~0.37s of round trip, so 31 checks collapsing to 24 scans is a
real saving.

## Step 6 — Inside one check

`invariants/conservation.py`

Every invariant is the same shape: `id`, `subject`, `severity`, `run(ctx)`.

```python
class DecompositionSum:
    severity = Severity.BLOCK

    def run(self, ctx):
        rows = ctx.fetch(self._sql.grouped(self._dim, ctx.window))
        grouped = sum(float(v) for _, v in rows if v is not None)
        total = scalar(ctx.fetch(self._sql.total(ctx.window)))
        delta = relative_delta(grouped, total)
        violated = delta > self._m.tolerance
        return CheckResult(..., verdict(violated, self.severity), self._detail(...))
```

`_detail` is not decoration. A finding that says "CON-01 failed" is an alert; a
finding that says *2,097,007.23 (10.45%) disappears in the traversal to regions*
is a reason to open the file. Every check spends real effort there.

## Step 7 — Compare against history

`invariants/temporal.py` · `run/history.py`

`Restatement` is the only check that writes, and the write is the point:

```python
current = _closed_periods(ctx, self._sql)
baseline = ctx.history.previous(self._m.name)
ctx.history.record(self._m.name, current, ctx.now)
```

Without recording there is nothing to compare against next time, and the past
keeps changing unnoticed.

`_closed_periods` excludes both window edges — the current month is still
accumulating, and the oldest is clipped by a rolling lookback and would drift
every night. That second exclusion was added after a live run reported two
restated periods when only one was real.

`SnapshotStore` is declared in `invariants/base.py`, not imported from `run`,
so the dependency points inward.

## Step 8 — Report

`run/report.py` · `run/runner.py`

`sort_for_report` orders most-severe-first, then by id, so consecutive runs
diff cleanly. `markdown()` hides passing checks by default; `slack_blocks()`
caps at ten findings.

Exit codes: `0` clean, `1` warnings only, `2` blocking.

---

## The demo, and why it is structured that way

`demo/data.py` generates the dataset and knows nothing about where it goes.
`demo/seed.py` writes it to DuckDB; `demo/load_snowflake.py` writes it to
Snowflake. **The two runs are only comparable if they see identical rows**, so
generation is deterministic and asserted in tests rather than trusted.

`demo/load_snowflake.py` is the only code in the repository that writes. It
talks to the connector directly rather than through `SnowflakeAdapter`, which
refuses non-queries, and it plans first and writes only with `--yes`.

Two Snowflake differences surface here rather than in the adapter:
`GENERATOR(ROWCOUNT)` in place of `generate_series`, and `SEQ4()` aliased once
in a subquery rather than referenced per column — Snowflake does not guarantee
a stable value across references within a row.

## Tests

```
tests/unit/          against a FakeAdapter returning canned rows — milliseconds
tests/integration/   against real DuckDB with seven planted defects — ~14s
```

159 tests. The integration suite asserts each planted defect is found *and
named*, then restates a closed month and asserts the second run notices,
including that metrics the backfill did not touch stay quiet.
