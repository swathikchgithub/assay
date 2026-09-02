# 4 · Design decisions and alternatives

Two parts. First: alternatives to *the product* — approaches that already exist
and why none of them covers this. Second: alternatives to *the design* —
decisions taken inside the codebase, in ADR form.

---

# Part one · Alternatives to the product

## A-1 · Column-level data quality tests

**dbt tests, Great Expectations, Soda.** Assert on columns: `not_null`,
`unique`, `accepted_values`, row counts in range, freshness.

These are good and Assay does not replace them. They are also, for this
problem, insufficient in a way that is easy to miss:

> **None of the seven defects in the demo has a bad column.**

`active_users` mislabelled additive has perfectly valid columns. A line-item
join that inflates revenue 150% has perfectly valid columns. A promo batch file
loaded twice produces perfectly valid rows. A region lookup missing a row is
`not_null`, `unique`, and complete by every column-level measure.

These are **semantic** defects. They exist in the relationship between a
definition and the data, not inside a column. Column tests cannot see them
because they never compute the metric.

**Verdict:** complementary. Assay operates one layer up. A project should have
both.

## A-2 · Metric anomaly detection

**Monte Carlo, Anomalo, Metaplane, Sifflet.** Monitor metric time series
statistically; alert when a number moves unusually.

The blind spot is structural:

> **Anomaly detection catches *changes*. It cannot catch *wrongness*.**

A metric that has been wrong since the day it was written never moves, so it
never alerts. The fan-out in the demo inflates revenue by 150% *consistently*,
every day, and looks perfectly stable to any statistical monitor. So does the
missing region. So does the double-counted promo. A monitor trained on wrong
data learns that wrong is normal.

Assay contains two checks of this kind — TMP-02 (envelope) and TMP-04
(discontinuity) — and they are deliberately the weakest in the suite, `warn`
and `note` severity. The load-bearing checks are the structural ones, which
compare the data against what the *definitions claim*, and therefore fire on
day one on a defect that has always been there.

**Verdict:** complementary, and Assay is the half that finds long-standing
errors. Anomaly detection finds new ones.

## A-3 · A golden-query regression suite

The conventional prescription: write down the questions that matter and the
answers you expect, and assert on them in CI.

This is the approach Assay was designed *against*, and the reasoning is the
core of the product:

> **A hand-written test suite rots at exactly the rate the definitions do.**

Every metric change invalidates some unknown number of goldens, by hand, and
the person changing the metric is not the person who wrote them. This is the
same rock that hand-authored semantic layers die on — a large up-front
authoring project nobody finishes and nobody maintains — and bolting a test
suite onto it doubles the maintenance burden rather than solving it.

A metric contract already contains everything needed to know what must be true
of it. `additivity: additive` is a claim. A declared join is a claim that the
traversal is many-to-one. Generating checks from claims means a definition
change regenerates its own checks and there is nothing to keep in sync.

**Verdict:** rejected. See [D-01](#d-01--generate-checks-from-contracts).

## A-4 · Semantic layer governance

**dbt Semantic Layer, Cube, AtScale, LookML.** Define metrics once, compute
identically everywhere.

These solve consistency, and consistency is genuinely valuable. But:

> **A governed metric guarantees consistency, not correctness.**

Every dashboard showing the same wrong number consistently is precisely what
governance buys. The definition is the thing that might be wrong, and a layer
whose job is to apply the definition faithfully cannot audit it.

**Verdict:** complementary, and the natural substrate. Assay reads dbt's
semantic manifest as an input rather than competing with it.

## A-5 · Build the natural-language interface first

The obvious product instinct: the demo is seductive, executives want to type a
question, and text-to-SQL over a clean schema is a weekend project.

Two objections. The interface is a **commodity** — every BI vendor shipped one,
and it is six months from being table stakes. And it makes the underlying
problem worse rather than better, because it multiplies the number of numbers
reaching people who cannot check them.

**Verdict:** deferred to P3, deliberately last. See
[D-07](#d-07--the-planner-ships-last).

## A-6 · Do nothing

The honest baseline. Most organisations run this way: numbers are checked when
someone senior notices something odd.

It has a real cost, but the cost is invisible and arrives late, which is why it
persists. The whole purpose of P0 as a cheap experiment is to find out whether
a team, shown a genuine defect in a number they ship, actually cares. If they
shrug, this alternative wins and the product should not exist.

---

# Part two · Design decisions

## D-01 · Generate checks from contracts

**Decision.** `invariants/registry.py` turns a contract set into a check suite.
No check is written per customer, per metric, or per project.

**Alternatives.** A golden-query suite ([A-3](#a-3--a-golden-query-regression-suite));
a rules DSL customers author; an LLM that proposes checks.

The DSL loses on the same maintenance argument as goldens. LLM-proposed checks
fail a harder test: a check must be *trustworthy enough to block a deploy*, and
a probabilistic proposal is not. Assay uses model judgment nowhere in P0, and
the one narrative check planned for P3 is deliberately non-blocking — **a model
is never the sole authority for a blocking decision.**

**Consequence.** The guards on generation carry as much weight as the
generators. Assay refuses to emit CON-01 for a non-additive metric, because
summing groups of a distinct count fails forever and means nothing, and a check
that cries wolf teaches people to ignore the whole report.

## D-02 · Ports and adapters, three protocols

**Decision.** `ContractSource`, `WarehouseAdapter`, `SnapshotStore`. Nothing in
`contracts`, `engine`, or `invariants` imports from `run`.

**Alternatives.** Direct driver calls inside checks — simpler, and it would
have made the Snowflake port a rewrite. A full ORM — heavier than the six
query shapes justify.

**Consequence, and the evidence it was right.** The Snowflake adapter was added
with **zero changes to any invariant**, and the report it produced against a
live account was byte-for-byte identical to DuckDB's. The unit suite runs
against a fake adapter in milliseconds; the same checks run against production.
That is the payoff, and it was measurable rather than theoretical.

## D-03 · Compile to the customer's warehouse

**Decision.** Assay never owns query execution. It emits SQL for whatever the
customer already bought.

**Alternatives.** Ship an engine; extract to a local store and check there.

Extraction is tempting — full control, no warehouse cost — and wrong, because
the thing being verified is *the warehouse's own answer*. Checking a copy
verifies the copy.

**Consequence.** Every dialect difference must be handled explicitly, and each
of them produces a wrong answer rather than an error if it is not.

## D-04 · History lives in SQLite, not the warehouse

**Decision.** Per-period metric snapshots and the check log go to a local
SQLite file.

**Alternatives.** A results schema in the warehouse — natural, queryable,
shareable.

Rejected because it would require **write grants**, and that trades away the
property that makes Assay safe to point at production. A verification layer
that can write is one that can corrupt what it verifies.

**Consequence.** History is per-installation rather than shared. For P0's
nightly job that is fine. When P1 needs an answer log several people read, this
decision gets revisited — and the honest answer will be a separate database,
not the warehouse under audit.

## D-05 · Read-only by construction

**Decision.** Enforced twice: connect with a `SELECT`-only role, and reject any
statement that is not `SELECT`/`WITH` before it is sent.

**Alternatives.** Trust the role alone. Reasonable — but the guard is free, and
Assay generates 100% of its own SQL, so a non-query statement means something
has already gone wrong upstream.

**Consequence, including a real cost.** The guard blocks `SHOW` and `DESCRIBE`,
which *are* reads. That is the guard being blunt rather than safe, and it means
Assay cannot inspect warehouse settings — such as `auto_suspend`, which
dominates its own running cost. Widening it to allow `SHOW`/`DESCRIBE`/`EXPLAIN`
is a known open decision.

## D-06 · A check names metrics, never tables

**Decision.** Query builders take metrics and dimensions. Join paths are
resolved from the contract.

**Alternatives.** Let checks write SQL directly — far more flexible.

That flexibility is exactly what must be given up. If a check could write its
own `FROM`, a fan-out would be an accident to detect rather than a claim to
test. Because the traversal comes from the contract, `CON-04` can ask a precise
question: *does this declared many-to-one join actually behave that way?*

**Consequence.** `total()` and `grouped()` deliberately build different `FROM`
clauses. If they shared one, their sums would agree by construction and CON-01
would be tautological. The difference between them is the check.

## D-07 · The planner ships last

**Decision.** P0 has no language model and no query interface. The natural
language planner is P3.

**Alternatives.** Ship the interface first, since it demos ([A-5](#a-5--build-the-natural-language-interface-first)).

**Consequence.** P0 is unglamorous and, deliberately, cheaply falsifiable. If a
team is indifferent to genuine defects in numbers they already ship to
executives, nothing downstream matters — and three weeks said so instead of
nine months.

## D-08 · Import definitions, do not ask for authoring

**Decision.** `DbtManifestSource` reads a dbt semantic manifest. YAML exists
for teams without dbt, not as the primary path.

**Alternatives.** Require a contracts file. Cleaner model, and it would have
killed the product — a large up-front authoring project is the rock every
metrics-layer tool has died on. **Cold start is the market, not an onboarding
step.**

**Consequence, and the known weak point.** dbt does not record additivity, so
it is inferred from aggregation type: `count_distinct` → non-additive, `sum` →
additive. That inference is what makes IDN-03 meaningful on an untouched
project, and it is also the most likely thing to be wrong. A `sum` over a daily
snapshot table is semi-additive; Assay will call it additive and then correctly
report that it does not roll up — a true finding, misattributed. The defect is
the missing label, not the metric.

## D-09 · A restricted evaluator, never `eval()`

**Decision.** Identity expressions are parsed to an AST and evaluated against a
node allow-list.

**Alternatives.** `eval()` with a restricted namespace — a known-porous
sandbox, and contract files are data that can be compromised.

**Consequence.** Only `+ - * /`, numeric literals, and metric names. Anything
else is a parse error rather than an execution.

## D-10 · A Python service, not dbt macros

**Decision.** Assay is a standalone process.

**Alternatives.** Implement checks as dbt tests and macros — zero new
infrastructure and it lands where the definitions already live.

Rejected on three counts. It locks the product to dbt, when the same checks
should run against Cube or a hand-written semantic layer. It cannot hold
cross-run state, and **restatement detection — the highest-value check in the
suite — requires knowing what the past looked like last night.** And it cannot
reconcile against an external system of record, which is P2.

## D-11 · Additivity is a contract term, not a hint

**Decision.** Every metric declares how it may be rolled up, and generation
branches on it.

**Alternatives.** Infer at query time; ignore it and always sum.

**Consequence.** The most valuable check in the identity family exists only
because of this field. `IDN-03` compares a metric rolled up from daily against
the same metric computed natively at month — which is a test of whether the
declared additivity is *true*. In the demo it catches a distinct count
declared additive, where the daily rollup is fifteen times the real figure and
nothing else in any tool would notice.

## D-12 · The clock is injected

**Decision.** `--as-of` overrides "now" everywhere.

**Alternatives.** Read the system clock; read the warehouse's clock.

The warehouse's clock is worse than it looks — a session timezone setting
should not change whether a freshness check passes.

**Consequence.** Temporal tests assert exact lag hours rather than
approximating, and a run is reproducible.

---

## Known open decisions

| | Question | Current state |
|---|---|---|
| O-1 | Should the read-only guard allow `SHOW`/`DESCRIBE`/`EXPLAIN`? | Blocked, which prevents Assay diagnosing warehouse cost settings |
| O-2 | Should the private key support a passphrase? | Not supported; unencrypted key at mode 600 |
| O-3 | Is tolerance per-metric, per-grain, or per-segment? | Per-metric; seasonal businesses may force the third |
| O-4 | Where does history live when several people read it? | SQLite; revisit at P1, and the answer is not the warehouse under audit |
| O-5 | What overhead ratio will a data platform lead accept? | Assumed ≤15%, unvalidated |
