# 5 · Invariant reference

Eleven checks in P0, across three families. Each is generated from a contract;
none is written by hand.

**Severity.** `block` fails the run (exit 2), `warn` renders on the report,
`note` is telemetry.

**Trigger.** `answer_time` runs against the current window; `scheduled` runs
against history.

---

## Conservation — does the arithmetic close?

The highest-yield family. Catches partitions silently dropped, unattributed
mass, over-aggressive filters, and traversals that multiply the base table.

### CON-01 · Decomposition sum · `block`

The parts must sum to the whole. Compares a metric grouped by one dimension
against its ungrouped total.

The two statements deliberately differ by the traversal needed to reach the
dimension — if they shared a `FROM`, they would agree by construction. That
difference is the check.

*Generated for:* additive metrics × each dimension.
*Not generated for:* non-additive metrics — summing groups of a distinct count
would fail forever and mean nothing.

> `net_revenue by region` — grouping by region accounts for 17,966,839.32 of
> 20,063,846.55 — 2,097,007.23 (10.45%) disappears in the traversal to regions

Two shapes of failure, reported differently: value *lost* (an inner join to an
incomplete dimension table) and value *inflated* (a traversal to a finer grain).

### CON-02 · Null mass · `warn`

How much of the metric has no value for this dimension. A "revenue by region"
slice that quietly omits 5% of revenue is not wrong, but it misleads.

*Generated for:* additive metrics × each dimension.

> `net_revenue by segment` — 5.19% of net_revenue has no segment
> (1,040,746.80 unattributed)

### CON-03 · Filter mass · `warn`

How much the metric's own filter removes. Catches a predicate far more
aggressive than intended.

*Generated for:* metrics declaring a `where`.
*Threshold:* 50% by default.

### CON-04 · Row conservation · `block`

A declared traversal must not multiply the base table. Compares row count
before and after one join.

Fan-out only — a join that *drops* rows is CON-01's finding, not this one.

*Generated for:* each join not marked `required`.

> `net_revenue -> order_items` — fan-out: joining order_items turns 16,110 rows
> into 40,341 (2.50x) — every additive measure over this path is overstated

---

## Identity — do the metrics agree?

### IDN-01 · Declared identity · `block`

A relationship the contract asserts must hold: `net = gross − discounts`,
`churn + retention = 1`.

Expressions are parsed to an AST and evaluated against an allow-list of
`+ - * /`, numeric literals, and metric names. Never `eval()`.

*Generated for:* each declared identity.

> `net_is_gross_less_discounts` — net_revenue = gross_revenue − discount_total:
> 20,063,846.55 vs 19,451,247.03 (3.15% apart)

### IDN-02 · Derived identity · `warn`

The same machinery, auto-extracted from a metric's `derived` expression.
Reported separately because provenance matters when triaging: a *declared*
identity failing means two people disagree; a *derived* one failing means a
component changed meaning under a metric that still looks healthy.

*Generated for:* metrics declaring `derived`.

### IDN-03 · Cross-grain consistency · `block`

**The most valuable check in the suite.** A metric computed daily and rolled up
must equal the same metric computed natively at month.

Anything grain-dependent fails here and nowhere else: a distinct count
mislabelled additive, a deduplication that only works inside a day, a
semi-additive snapshot being summed.

Nothing is wrong with the data or the SQL. The metric is *labelled* wrong, and
every consumer summing a daily series is told to over-count.

*Generated for:* metrics with a defined rollup rule (additive → sum,
semi-additive → last).
*Not generated for:* non-additive metrics — no legal rollup to compare against.

> `active_users` — 2026-01: daily values rolled up give 14,193.00, computed
> natively at month gives 900.00 (1477.00% apart) — `active_users` is declared
> additive but does not roll up that way

---

## Temporal — what does history say?

### TMP-01 · Freshness · `warn`

The newest row is within the metric's declared SLA. Catches answering
confidently from a pipeline that stopped on Friday.

Warehouse timestamps are naive UTC and a caller's clock may be timezone-aware;
both sides are normalised before comparison.

*Generated for:* metrics declaring `freshness_sla_hours`.

### TMP-02 · Envelope · `warn`

The newest closed period sits inside a robust band of its own history.

Median and MAD rather than mean and standard deviation: one restatement or
outage would drag a mean-based band wide enough to hide the next one. A flat
series falls back to a proportional band, since a zero-width band fails on any
movement at all.

*Requires:* 7 closed periods before it enforces anything, so a new metric does
not fire against an uncalibrated tolerance.

### TMP-03 · Restatement · `block`

**A closed period's value has changed since it was last observed.**

Every warehouse rewrites the past constantly — late-arriving rows, backfills,
restated sources — and almost no tool reports it. This is the check that makes
P1's recall service possible, and on its own it is the most surprising thing
P0 reports.

The check writes as well as reads: it records the current series so the next
run has a baseline. That write is the point, not a side effect.

Both window edges are excluded. The current month is still accumulating; the
oldest month is clipped by a rolling lookback and would shift slightly every
night. A restatement check that fires on a calendar boundary is one nobody
reads.

*Requires:* a previous run. First run records a baseline and skips.

> `net_revenue` — 1 closed period(s) changed since the last run — 2026-04 was
> 1,116,524.68, is now 3,194,191.29 (186.08%)

### TMP-04 · Discontinuity · `note`

A step change unlike any other step in the series. Overlaps TMP-02 by design
and is deliberately telemetry-only.

---

## What is deliberately absent

| Family | Why not in P0 |
|---|---|
| `STR-01..09` structural | Need the plan algebra; enforced implicitly by generation guards until then |
| `REC-01..02` reconciliation | Need a configured external system of record — P2 |
| `DRF-01..04` change | Need definition-change hooks in CI — P2 |
| `NAR-01..04` narrative | Need an answer to have prose, which needs a planner — P3 |
