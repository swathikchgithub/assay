# 9 · Roadmap

## Where P0 stands

**Done and verified.** Eleven generated checks find all seven planted defects,
on DuckDB and on a live Snowflake account, byte-identically, under a read-only
role. Setup failures diagnose themselves. 159 tests.

**Not done, and it is the only thing that matters.** Every defect found so far
was planted by us. The open question is whether these checks find anything a
team *cares about* in their own numbers.

That needs `--dbt-manifest` against a real project. The first thing expected to
break is additivity inference: dbt does not record it, so it is inferred from
aggregation type, and a `sum` over a daily snapshot table will be called
additive and then correctly reported as not rolling up. A true finding,
misattributed — the defect is the missing label, not the metric.

## Phases

| Phase | Ships | Proves |
|---|---|---|
| **P0** ✓ | Invariant engine over existing contracts. Conservation, identity, temporal. Nightly, no interface beyond a message | Generated checks find real defects. **If a team shrugs, stop here** |
| **P1** | Proof cards, answer log, recall service. TMP-03 wired to notifications | People care that Monday's number moved — the retention hypothesis |
| **P2** | Evidence miner and confirmation loop. Reconciliation against an external system of record | Semantics can be *earned* rather than authored — the cold-start hypothesis, and the moat |
| **P3** | Planner, type gate, refusal and disambiguation. Structural and narrative checks | The verification layer makes natural-language analytics trustworthy |

The ordering is the thesis as a roadmap: **the planner ships last, because the
interface was never the hard part.**

## P1 — making an answer live

Today an answer is fire-and-forget. TMP-03 already knows the past changed; P1
connects that to the people who acted on it.

- **Proof card** — value, plan, SQL, per-source freshness, check results,
  excluded-row accounting. Permalinked and reproducible.
- **Answer log** — every answer, and who saw it, where.
- **Recall service** — when a definition or source changes, recompute the
  affected answers and notify whoever consumed them.

> *"The 4.2% churn in the deck you shared on Monday is now 6.8% — a Salesforce
> backfill added 340 accounts to the Q3 denominator."*

A message saying a number moved is an alert. A message saying **why** is a
reason to keep the product. Notification quality is the whole bet.

This is where [D-04](04-design-decisions.md#d-04--history-lives-in-sqlite-not-the-warehouse)
gets revisited: several people reading one answer log needs shared storage. The
answer is a separate database, not the warehouse under audit.

## P2 — earning the semantics

The cold-start problem is the market, not an onboarding step.

- **Evidence miner** — derive candidate entities, join paths, and metrics from
  query logs, the dbt manifest, BI dashboard definitions, foreign keys, column
  profiles. Each candidate carries its evidence and a confidence score.
- **Confirmation loop** — route a candidate to its likely owner when a question
  first depends on it. One decision, in context. Unconfirmed definitions stay
  usable but are stamped *provisional* on every answer.
- **Reconciliation (REC-01/02)** — agreement with an independent system of
  record. The highest-trust check available, and the only one that catches a
  pipeline-wide error where every internal check agrees with itself.

**Precision over recall, without exception.** A confidently wrong confirmed
metric is worse than no metric, because it launders a bad definition through
the trust layer.

Query-log mining is a privacy surface: logs contain literal filter values,
routinely including personal data. The miner must extract query *shapes* and
discard literals at ingestion, not at storage. That is a hard boundary.

## P3 — the interface, last

- **Plan algebra** — a typed IR the planner emits. It can name metrics and
  dimensions and nothing else; no table names, no join clauses. Fan-out and
  grain errors become *unrepresentable* rather than detectable.
- **Type gate (STR-01..09)** — reject illegal plans before the warehouse is
  touched.
- **Refusal and disambiguation** — the most valuable output is often no answer.
  Every refusal is a logged product signal.
- **Narrative checks (NAR-01..04)** — prose must be grounded in the rows
  returned. The one judge-based check stays non-blocking: **a model is never
  the sole authority for a blocking decision.**

## Smaller open work

| | Item | Notes |
|---|---|---|
| O-1 | Widen the read-only guard to `SHOW`/`DESCRIBE`/`EXPLAIN` | All reads; would let doctor check `auto_suspend`, which dominates running cost |
| O-2 | Encrypted private key (`private_key_file_pwd`) | Needed before production |
| O-3 | Tolerance granularity | Per-metric today; seasonal businesses may force per-segment |
| O-4 | CON-03 is metric-scoped | Becomes per-query at P3 |
| O-5 | Query amplification budget | Assumed ≤15% overhead, unvalidated |
| O-6 | More adapters | BigQuery, Databricks. Each is a `Dialect` plus an adapter and one branch in `targets.py` — no invariant changes |

## What would kill it

- A team is shown a genuine defect in a number they ship to executives, and
  shrugs. ([A-6](04-design-decisions.md#a-6--do-nothing) wins.)
- The false-positive rate on real projects is high enough that reports get
  muted. Precision matters more than coverage.
- Warehouse overhead exceeds what a platform lead will accept.
- A platform vendor ships good-enough versions inside their own stack. The
  defence is being cross-platform and test-first, which a single-vendor
  incumbent structurally will not build.
