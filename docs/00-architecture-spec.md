# 0 · Architecture spec (P0–P3)

> **Status.** This is the origin design document for Assay, written before any
> code. It covers the whole arc — P0 through P3 — where the rest of `docs/`
> describes what is actually built today.
>
> Read it for the parts P0 does not yet reach: the plan algebra (§4), the
> invariant classes that arrive with later phases (§5.1, §5.4, §5.6, §5.7), the
> two-plane data model (§6), and the recall algorithm (§7.3) that turns
> `TMP-03` into a message someone acts on.
>
> Where this document and the P0 docs disagree, **the P0 docs are current**.
> Known drift is listed at the end.

---
## Problem & thesis

> A governed metric guarantees consistency, not correctness. Every dashboard showing the same wrong number consistently is exactly what governance buys you.

Natural-language analytics fails in production for a reason specific to this domain: **the person asking is the person least equipped to check the answer.** In every other application of a language model, a bad output announces itself — wrong code fails to compile, a bad paragraph reads wrong. A wrong number looks exactly like a right number. Trust therefore does not degrade gracefully; it collapses permanently after one bad answer that reached a boardroom.

The conventional response is a hand-authored semantic layer plus a review process. That fails for a different reason: the semantic layer does not get written. Metrics-layer products have died for eight years on the same rock — a multi-month YAML-authoring project no data team finishes, rotting faster than it accumulates. Adding CI to an artifact that does not exist solves nothing.

### 1.1 Thesis

**Falsifiability over governance.** Trust is not a process promise about how definitions get approved. It is a technical property: every answer carries a proof a skeptic can attack, and the system continuously attempts to break its own answers before a human does.

Two consequences drive the entire architecture:

1. **Semantics are mined and confirmed, never authored.** Cold start is the market, not an onboarding step. The system arrives with a draft model of the business derived from artifacts that already exist, and asks for confirmation one decision at a time.
2. **Correctness checks are derived from the semantics, not typed by hand.** A hand-written golden-query suite rots for precisely the same reason the ontology does. Checks that are generated from metric definitions maintain themselves.

### 1.2 Non-goals

- **Not a BI tool.** No dashboards, no visual explore. Assay annotates answers produced elsewhere.
- **Not a query engine.** It compiles to the warehouse or semantic layer the customer already bought.
- **Not a data catalog.** Definitions here exist to be executed and tested, not browsed.
- **No chat interface in v1.** The interface is a commodity six months after launch; §10 deliberately schedules the planner last.

## System overview

A question becomes a typed *plan*, never raw SQL. The plan is checked against the contract set before anything touches the warehouse, compiled, executed, then attacked by a generated invariant suite. What survives becomes an answer with a proof card; what does not becomes a refusal, which is itself a logged product signal.

### 2.1 Components

- evidence-minerDerives candidate entities, join paths, and metric definitions from warehouse query logs, the dbt manifest, BI dashboard definitions, foreign keys, and column profiles. Emits candidates with evidence and a confidence score. Never blocks on human authoring.
- confirmation-loopRoutes a candidate to its likely owner at the moment a question first depends on it. One decision, in context. Unconfirmed definitions remain usable but are stamped *provisional* on every answer that touches them.
- plannerLanguage model constrained to emit plan IR (§4). Its only job is intent resolution — which metric, which slice, which window.
- type-gateDeterministic validation of a plan against the contract set. Rejects structurally; never asks a model whether a plan is sane.
- compilerPlan → SQL, or plan → a host semantic layer's API (§8). Resolves join paths; the plan itself never names a table.
- invariant-engineGenerates and runs the check suite (§5). The core IP.
- proof-cardThe answer artifact: value, plan, SQL, per-source freshness, check results, provisional-definition flags, excluded-row accounting. Permalinked and reproducible.
- recall-serviceWatches for restatements and definition changes, recomputes affected answers, and notifies the humans who already consumed them (§7.3).
- narrative-judgeVerifies that prose accompanying an answer is grounded in the rows actually returned (§5.7).

## Semantic contracts

The contract set is the vocabulary the plan algebra is closed over. It is stored as versioned files in a git repository and compiled into an in-memory index at service start. Nothing in it is authored from scratch by a human; everything begins as a mined candidate (§2.1).

### 3.1 Objects

```
entity        id, name, grain_key[], source_ref, synonyms[],
              owner, state, confidence, version

relationship  from_entity, to_entity, join_keys[],
              cardinality: one_to_one | many_to_one | many_to_many,
              traversal_safe: bool,        -- derived, see STR-07
              evidence[], state, confidence

dimension     id, entity_id, name, type, synonyms[],
              domain: enum[] | range | open,
              domain_refreshed_at

metric        id, name, expression, base_entity,
              additivity: additive | semi_additive | non_additive | derived,
              min_grain: TimeGrain,
              unit: money(ccy) | count | ratio | duration,
              allowed_dims[],              -- derived from reachability
              tolerance,                   -- relative, used by CON/REC/TMP
              owner, state, version, supersedes

identity      lhs: MetricRef, rhs: Expr(MetricRef...),
              tolerance, provenance: declared | derived

recon_pair    metric_id, external_source, key_mapping,
              tolerance, expected_lag
```

### 3.2 Additivity is a contract, not a hint

Most incorrect analytics answers are grain errors wearing a correct-looking number. The additivity class of a metric is therefore load-bearing: it determines which rollups the type gate permits at all.

| Class | Rollup rule | Example | Failure it prevents |
|---|---|---|---|
| additive | Sum across every dimension including time | `net_revenue` | — |
| semi_additive | Sum across non-time dimensions; `last` or `first` across time | `active_seats`, account balance | Summing a month of daily snapshots into a 30× overstatement |
| non_additive | Never summed. Recomputed from its own components at the target grain | `churn_rate`, `distinct_users` | Averaging rates across regions of unequal size; adding distinct counts |
| derived | Recomputed from constituent metrics after those are rolled up | `arpu = revenue / users` | Rolling up a ratio computed per-row |

### 3.3 Definition state

Every contract object carries a state. `provisional` is the state that makes cold start survivable — the system is useful before anyone has confirmed anything, and honest about it on every answer.

```
candidate ──(surfaced to owner)──▶ provisional ──(confirmed)──▶ confirmed
     │                                   │                          │
     └──(rejected)──▶ discarded          └──(contradicted)──▶ disputed
                                                                    │
                                            (superseded by version) ▼
                                                              deprecated
```

A `disputed` definition — two owners assert incompatible meanings — does not block answers. It forces the plan to name which variant it used, and the proof card to say so. Escalation is a product feature, not an error state (§9).

## Plan algebra

The planner emits a plan in a closed, typed intermediate representation. The design constraint that produces every downstream benefit: **a plan can name metrics and dimensions, and nothing else.** No table names, no join clauses, no raw expressions, no SQL fragments. Whole classes of error — fan-out, wrong join key, silent grain mismatch — become unrepresentable rather than detectable.

### 4.1 Grammar

```
Plan       := Query | Compare | Trend | Rank | Decompose

Query      := select:  [MetricRef]+
              by:      [DimRef]*
              where:   [Filter]*
              time:    TimeSpec
              limit:   Int?

Compare    := base: Query, against: Against,
              mode: delta | ratio | pct_change
Against    := TimeShift{periods: Int}
            | Segment{filter: Filter}
            | Target{ref: TargetRef}

Trend      := metric: MetricRef, grain: TimeGrain, window: TimeSpec,
              smoothing: none | trailing(n)

Rank       := query: Query, order: MetricRef, dir: asc | desc,
              n: Int, ties: dense | competition

Decompose  := metric: MetricRef, over: [DimRef]+,
              method: contribution | mix_rate

MetricRef  := "metric:" Ident "@" Version
DimRef     := "dim:" Entity "." Ident
Filter     := DimRef Op Value
Op         := eq | neq | in | not_in | gt | gte | lt | lte
            | between | is_null | not_null | matches

TimeSpec   := Absolute{start, end}          -- half-open [start, end)
            | Relative{anchor: TimeGrain, offset: Int, calendar: CalendarRef}
            | ToDate{anchor: TimeGrain, as_of: Date}
TimeGrain  := day | week | month | quarter | year   -- calendar-qualified
```

> **Fiscal calendars are first-class.** `Relative` carries a `CalendarRef` because “last quarter” means different things to Finance and to Sales, and a silent Gregorian default is a reliable way to lose an executive permanently. A plan with an unresolved calendar fails STR-08.

### 4.2 Worked example

“How did enterprise net revenue by region do last quarter versus the quarter before?”

```
{
  "kind": "compare",
  "mode": "pct_change",
  "base": {
    "kind": "query",
    "select": ["metric:net_revenue@v4"],
    "by":     ["dim:account.region"],
    "where":  [["dim:account.segment", "eq", "Enterprise"]],
    "time":   { "kind": "relative", "anchor": "quarter",
                "offset": -1, "calendar": "cal:finance_fy" },
    "limit":  50
  },
  "against": { "kind": "time_shift", "periods": -1 }
}
```

Note what is absent: no `FROM`, no `JOIN`, no `GROUP BY`. The compiler resolves `account.region` to a join path from the `net_revenue` base entity, and STR-06/07 guarantee that path is unique and fan-out-safe before a single byte is scanned.

### 4.3 Canonicalization

Every accepted plan is normalized to a canonical form and hashed. Canonicalization is not an optimization — three separate mechanisms depend on it, and §7.3 is impossible without it.

1. Resolve every `MetricRef` and `DimRef` to a pinned version.
2. Rewrite `TimeSpec` to an absolute half-open interval in UTC, tagged with the resolved calendar id.
3. Sort `where` by `(dim, op, value)`; sort `by` and `select` lexically.
4. Normalize filter values to the dimension's canonical domain member (so `"NE"` and `"Northeast"` converge).
5. Strip presentation-only fields that do not change the result set.
6. Hash the residue → `plan_hash`.

Consequences: **(a)** the hash is a correct cache key; **(b)** it is the join key of the dependency index that drives retroactive invalidation; **(c)** counting distinct hashes per question cluster measures real coverage rather than question volume.

### 4.4 Type rules

These run before compilation, in order, and are pure functions of the plan and the contract set — no warehouse access, single-digit milliseconds. All are block.

| ID | Rule | Rejects |
|---|---|---|
| STR-01 | Metric resolvable and active | A hallucinated metric name, or one deprecated three versions ago |
| STR-02 | Dimension reachable from the metric's base entity | Slicing revenue by a support-ticket attribute with no defined path |
| STR-03 | Requested time grain ≥ `metric.min_grain` | Asking for daily values of a metric only defined monthly |
| STR-04 | Rollup legal for the additivity class (§3.2) | Summing a semi-additive snapshot across time; averaging a rate |
| STR-05 | Filter values are members of the dimension domain | `region = 'Northeast'` when the column holds `'NE'` — silently returns zero rows |
| STR-06 | Join path exists and is unique | Two valid paths from orders to region with different meanings — refuse and disambiguate, never pick |
| STR-07 | Every traversal is many-to-one in the direction taken | The fan-out that doubles revenue when a line-item table joins in |
| STR-08 | Calendar and timezone resolved to one convention | “Last quarter” silently meaning Gregorian for a company on a 4-4-5 fiscal year |
| STR-09 | Asker is entitled to every referenced entity | Row-level security bypassed because the semantic layer, not the warehouse, resolved the join |

A rejection is not an error page. It carries a `reason_code` and, where possible, a repair proposal — STR-05 offers the nearest domain member, STR-06 offers the competing paths as a disambiguation question. Every rejection is written to the coverage log (§6.2), which is how the product learns what it cannot yet answer.

## Invariant taxonomy

An invariant is a machine-checkable assertion about a metric, a plan, or an answer. Most are *generated* from the contract set rather than written, which is what distinguishes this from a golden-query suite that decays.

**Invariant record**

```
invariant  id, class, subject_type: metric | plan | answer,
           trigger:  plan_time | answer_time | scheduled | on_change,
           severity: block | warn | note,
           tolerance, provenance: generated | declared | learned,
           enabled_after: Int      -- observations before enforcement
```

block fails the answer and produces a refusal. warn renders on the proof card in the reader's language. note is telemetry only. A newly generated invariant starts at `note` and promotes itself once `enabled_after` observations establish a stable baseline — a check that fires on day one against an uncalibrated tolerance trains people to ignore checks.

### 5.1 Class I — Structural

The nine rules of §4.4, restated here as invariants so they share a result table with everything else. Trigger `plan_time`, severity `block`, cost zero warehouse bytes.

### 5.2 Class II — Conservation

Assertions that the arithmetic of the result set is closed. These catch the largest share of real defects and are cheap when folded into the same scan via grouping sets.

| ID | Invariant | Trigger | Default | Catches |
|---|---|---|---|---|
| CON-01 | Decomposition sum: Σ over all members of each grouped dimension equals the ungrouped total within tolerance | answer_time | block | A dropped partition, an inner join that ate rows, a filter applied to one branch only |
| CON-02 | Null mass: fraction of base rows with a null on any grouped dimension | answer_time | warn | “Revenue by region” quietly omitting 6% of revenue that has no region |
| CON-03 | Filter mass: fraction of rows removed by each filter, individually | answer_time | warn | An over-restrictive filter the asker did not intend — 99% removal on one predicate |
| CON-04 | Row conservation across join: base-entity row count unchanged by the resolved path | answer_time | block | Fan-out that STR-07 could not prove statically (many-to-many resolved at runtime) |

### 5.3 Class III — Algebraic identity

Relationships between metrics that must hold at any grain where both sides are legal.

| ID | Invariant | Trigger | Default | Catches |
|---|---|---|---|---|
| IDN-01 | Declared identity holds — `gross − discounts = net`, `churn + retention = 1` | scheduled | block | Two metrics edited independently until they no longer describe the same world |
| IDN-02 | Derived identity, auto-extracted where one metric's expression references another | scheduled | warn | A component metric changing meaning under a derived metric that still looks fine |
| IDN-03 | Cross-grain consistency: the metric computed at day and rolled to month equals the metric computed natively at month | scheduled | block | Grain-dependent deduplication — the single most common silent error in warehouse metrics |

### 5.4 Class IV — Reconciliation

Agreement with an independent system of record. The highest-trust check available and the most expensive; scheduled, never per-answer.

| ID | Invariant | Trigger | Default | Catches |
|---|---|---|---|---|
| REC-01 | Metric agrees with its external source of record (general ledger, payment processor, CRM) within tolerance and expected lag | scheduled | block | Pipeline-wide error invisible from inside the warehouse, because every internal check agrees with itself |
| REC-02 | Duplicate-definition agreement: two contracts mined as the same concept produce the same number | scheduled | warn | The three-different-revenue-numbers problem, detected before an executive finds it |

### 5.5 Class V — Temporal

| ID | Invariant | Trigger | Default | Catches |
|---|---|---|---|---|
| TMP-01 | Freshness: max source lag ≤ the metric's declared SLA | answer_time | warn | Answering confidently from a pipeline that stopped running on Friday |
| TMP-02 | Envelope: value within a seasonality-adjusted, MAD-based band of its own history | answer_time | warn | An order-of-magnitude break that no structural check can see |
| TMP-03 | **Restatement**: a previously computed historical value has changed since last observed | scheduled | block | Late-arriving data and backfills silently rewriting the past — the trigger for §7.3 |
| TMP-04 | Discontinuity: step change not attributable to a registered event (migration, pricing change, acquisition) | scheduled | note | A deploy that changed a definition without changing a definition file |

TMP-03 deserves emphasis. Every analytics stack recomputes the past constantly and tells no one. It is the highest-value check in this taxonomy precisely because no existing tool reports it to the humans it affects.

### 5.6 Class VI — Change

| ID | Invariant | Trigger | Default | Catches |
|---|---|---|---|---|
| DRF-01 | Blast radius resolved: every plan and answer depending on the changed object is enumerated | on_change | block | A definition edit shipping with no idea what it moves |
| DRF-02 | Historical backtest: N prior periods recomputed under old and new definitions, deltas reported on the pull request | on_change | block | A “clarification” that moves Q3 churn from 4.2% to 6.8% |
| DRF-03 | Owner approval recorded for any confirmed-state definition change | on_change | block | Unattributed edits to a number the board sees |
| DRF-04 | Upstream schema contract: referenced columns exist with compatible type and cardinality | scheduled | block | A warehouse migration that turns a many-to-one join into many-to-many |

### 5.7 Class VII — Narrative faithfulness

Where an answer is accompanied by prose, the prose is an assertion set and gets checked like one. Deterministic extraction first, model judgment only for what extraction cannot decide.

| ID | Invariant | Method | Default | Catches |
|---|---|---|---|---|
| NAR-01 | Every figure in the prose appears in the result set | Numeric extraction + set membership | block | A fabricated or rounded-into-wrongness number in the summary |
| NAR-02 | Every comparison asserted was actually computed | Plan inspection | block | “Up from last year” when no prior-year query ran |
| NAR-03 | Every causal claim is backed by a decomposition that was executed | Grounded judge | warn | “Driven by enterprise renewals” asserted from a single aggregate |
| NAR-04 | Active warnings propagate into the prose | Deterministic | block | A confident sentence over a result with 6% unattributed rows |

NAR-03 is the only invariant in the taxonomy that requires model judgment, and it is deliberately non-blocking. The design rule: **a model may never be the sole authority for a blocking decision.**

## Data model

Two planes with different persistence characteristics. The **definition plane** is content-addressed and lives in git — immutable versions, reviewable diffs, no in-place edits. The **runtime plane** is Postgres, holding questions, plans, answers, and the evidence trail. They meet at exactly one place, and that junction is what makes retroactive invalidation possible.

### 6.1 Definition plane

```
-- git-backed, content-addressed, immutable versions

entity(id PK, name, grain_key[], source_ref, synonyms[],
       owner, state, confidence, version)

relationship(id PK, from_entity FK, to_entity FK, join_keys[],
             cardinality, traversal_safe, state, confidence)

dimension(id PK, entity_id FK, name, type, synonyms[],
          domain jsonb, domain_refreshed_at)

metric_version(id PK, metric_id, version, expression, base_entity FK,
               additivity, min_grain, unit, tolerance,
               allowed_dims[], owner, state, created_at,
               supersedes FK NULL)
  UNIQUE(metric_id, version)

identity(id PK, lhs_metric FK, rhs_expr, tolerance, provenance)

recon_pair(id PK, metric_id FK, external_source, key_mapping jsonb,
           tolerance, expected_lag)

evidence(id PK, subject_id, subject_type,
         kind: query_log | dbt_manifest | bi_dashboard | fk | profile,
         payload jsonb, weight, observed_at)

confirmation(id PK, subject_id, subject_type, decider,
             decision: confirm | reject | dispute, rationale, decided_at)
```

### 6.2 Runtime plane

```
-- Postgres

question(id PK, text, asker, workspace, source_channel, asked_at)

plan(id PK, question_id FK, plan_hash, ir jsonb,
     contract_set_version, status: accepted | refused, created_at)
  INDEX(plan_hash)

plan_dependency(plan_id FK, dep_type: metric_version | source_table,
                dep_id, pinned_version)
  PRIMARY KEY(plan_id, dep_type, dep_id)
  INDEX(dep_type, dep_id)          -- the reverse index; see §7.3

execution(id PK, plan_id FK, compiled_sql, engine, started_at,
          duration_ms, rows_scanned, rows_returned, result_ref)

answer(id PK, plan_id FK, execution_id FK, values jsonb, narrative,
       state, confidence, superseded_by FK NULL, computed_at)

answer_view(answer_id FK, viewer, channel, viewed_at)
  PRIMARY KEY(answer_id, viewer, viewed_at)

check_run(id PK, invariant_id, subject_type, subject_id,
          status: pass | warn | fail, observed, expected, delta,
          ran_at)
  INDEX(subject_type, subject_id, ran_at DESC)

refusal(id PK, question_id FK, reason_code, missing_concept,
        proposed_repair jsonb, refused_at)

notification(id PK, answer_id FK, recipient, cause_type, cause_id,
             old_value, new_value, sent_at, acknowledged_at NULL)
```

> `refusal` is not an error table. Grouping it by `missing_concept` produces a demand-ranked backlog of the semantics the business needs next — written by users, in their own words, without a single interview. It is the product's roadmap and one of its more defensible assets.

## Lifecycles

### 7.1 Answer state machine

### 7.2 Retention window

Answers do not stay live forever. Each carries a `recall_horizon` — default 90 days, extended when the answer was viewed in a high-stakes channel (a board deck export, a shared permalink) and shortened for exploratory sessions. Beyond the horizon an answer is archived: still readable, no longer recomputed.

### 7.3 Recall algorithm

```
on change(subject)                 -- metric_version | source_table

  1  deps    ← plan_dependency WHERE (dep_type, dep_id) = subject
  2  plans   ← distinct(deps.plan_id)
  3  live    ← answers WHERE plan_id ∈ plans
                 AND state ∈ {validated, stale}
                 AND computed_at > now() − recall_horizon
  4  mark live.state ← stale
  5  batches ← group live BY plan.plan_hash      -- §4.3 canonical form
  6  for each batch: execute ONCE, reuse result across its answers
  7  Δ ← |new − stored| / |stored|
  8  if Δ ≤ metric.tolerance:  state ← validated;  record check_run(pass)
     else:                     state ← invalidated; record check_run(fail)
  9  for each invalidated answer a:
       recipients ← distinct(answer_view WHERE answer_id = a)
       enqueue notification(a, recipient, old, new, cause = subject)
```

**Complexity**

- **Step 1** is `O(k)` in the number of dependents via `INDEX(dep_type, dep_id)` — never a scan over the answer table, which grows without bound.
- **Steps 5–6** make warehouse cost `O(distinct plan_hash)`, not `O(affected answers)`. Fifty executives who asked the same question in different words share one recompute. This is the entire practical payoff of canonicalization, and without it the recall service is unaffordable at any real scale.
- **Step 9** is `O(viewers)`, deduplicated per recipient per cause so one backfill produces one message, not forty.

> **Notification quality is the make-or-break.** A recall message must name the change, not the symptom: “*The 4.2% churn in the deck you shared on Monday is now 6.8% — a Salesforce backfill added 340 accounts to the Q3 denominator.*” A message that only says a number moved is an alert; a message that says why is a reason to keep the product.

## Compilation targets

The plan is engine-agnostic by construction. One compiler backend per target, each responsible for join-path resolution and additivity-correct rollup.

| Target | Emits | Who owns join resolution | Note |
|---|---|---|---|
| warehouse-sql | Dialect SQL (Snowflake, BigQuery, Databricks) | Assay | Full control; all of §5 available |
| dbt-semantic | MetricFlow query spec | Host | STR-06/07 delegated — still verified, never assumed |
| cube | Cube REST query | Host | Metric contracts imported from the Cube schema |
| cortex | Cortex Analyst semantic model call | Host | Assay runs as the verification layer over the vendor's own NLQ |

The `cortex` row is the commercially interesting one. When a customer has already bought a platform NLQ feature, Assay does not replace it — it supplies the precondition that makes the vendor's pitch true, by checking the vendor's answers. Compiling to a host semantic layer means delegating some structural guarantees; the compiler must then *verify* them from returned metadata rather than trust the host, or Class I silently degrades to nothing.

## Risks & open questions

- query amplificationConservation checks re-scan data and can multiply warehouse spend, which is the fastest way to get uninstalled. Mitigation: fold CON-01..03 into the primary scan using grouping sets rather than issuing separate queries; sample high-cardinality decompositions; run Class IV on a schedule with an explicit budget. **Open:** what the acceptable overhead ratio is before a data platform lead refuses. Assumed ≤15%; unvalidated.
- tolerance calibrationA check that fires constantly gets ignored, and an ignored check is worse than an absent one because it manufactures false confidence. Learned envelopes with an `enabled_after` gate are the design answer. **Open:** whether tolerance is per-metric, per-metric-per-grain, or per-metric-per-segment. Probably the second; the third may be unavoidable for seasonal businesses.
- refusal rateRefusing is correct and also the fastest way to lose a user. The north-star metric must be *answered without warning*, never *refusals avoided* — optimizing the latter reintroduces exactly the confident-wrong-answer behavior the product exists to prevent.
- mined semantics qualityPrecision over recall, without exception. A confidently wrong confirmed metric is worse than no metric, because it launders a bad definition through the trust layer. Candidates below a confidence floor should never be surfaced for confirmation at all.
- query-log privacyQuery logs contain literal filter values, which routinely include personal data. The miner must extract query *shapes* and discard literals at ingestion, not at storage. This is a hard boundary and belongs in the threat model, not the backlog.
- non-additive realityReal organizations are dominated by non-additive metrics. If the algebra makes those painful, teams will fake additivity to get answers — recreating the failure the product exists to prevent. **Open:** whether `non_additive` needs a first-class recomputation spec in the contract rather than the current implicit “recompute from components”.
- disputed ownershipWhen Finance and Product genuinely disagree, no technical mechanism resolves it. The system's job is to make the disagreement visible and force both variants to be named at query time. **Open:** whether a disputed metric should be answerable at all, or only answerable with an explicit variant selection.
- notification fatigueRecall is the differentiating feature and the one most likely to be muted. Needs severity thresholds tied to how the answer was consumed, not to raw delta magnitude — a 0.3% move in a board number matters more than a 40% move in an exploratory query nobody kept.

## Phasing

The ordering below is the thesis expressed as a roadmap: the planner ships last, because the interface was never the hard part.

| Phase | Ships | Depends on | Proves |
|---|---|---|---|
| P0 | Invariant engine over an existing dbt project. Classes II, III, V. Nightly, no interface beyond a Slack message. | Nothing — contracts imported from the dbt manifest | That generated checks find real defects in numbers a team already trusts. **If they don't, stop.** |
| P1 | Proof cards, answer log, recall service. TMP-03 wired to notifications. | P0 · §6.2 · §7.3 | That people care their Monday number moved — the retention hypothesis |
| P2 | Evidence miner and confirmation loop. Class IV reconciliation. | P1 | That semantics can be earned rather than authored — the cold-start hypothesis, and the moat |
| P3 | Planner, type gate, refusal and disambiguation UX. Classes I and VII. | P2 · a contract set worth planning against | That the verification layer makes NLQ trustworthy where it wasn't |

P0 is deliberately unglamorous and deliberately falsifiable. It carries no language model, no interface, and no new query surface — and if a team is indifferent to the defects it surfaces in numbers they already ship to executives, then the premise of everything after it is wrong and the cheapest possible experiment just said so.

---

## Drift from what is built

This document predates the implementation. Where P0 diverged, it diverged for
reasons recorded in [design decisions](04-design-decisions.md):

| Spec says | P0 does | Why |
|---|---|---|
| History in a runtime plane alongside answers | Local SQLite, snapshots and check log only | Writing to the warehouse needs write grants, which destroys the read-only posture ([D-04](04-design-decisions.md#d-04--history-lives-in-sqlite-not-the-warehouse)) |
| `STR-01..09` enforced by a type gate | Enforced implicitly by generation guards | No planner yet, so there is no plan to type-check |
| Plans, answers, proof cards, `answer_view` | Not built | P1 |
| Evidence miner, confirmation loop, `REC-01..02` | Not built | P2 |
| Planner, refusal log, `NAR-01..04` | Not built | P3 |
| `CON-03` per user filter | Per metric filter | No user filters without a planner |

The taxonomy ids are stable across both documents, so a check named here keeps
its name when it ships.

