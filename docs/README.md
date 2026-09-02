# Assay documentation

Ten documents. Read in this order if you are new; jump straight to the one
you need if you are not.

| # | Document | Answers |
|---|---|---|
| 1 | [Product requirements](01-prd.md) | What problem, for whom, and how we know it worked |
| 2 | [How it works](02-how-it-works.md) | The mechanism, end to end, without reading code |
| 3 | [Technical design](03-tdd.md) | Architecture, components, contracts, data model, diagrams |
| 4 | [Design decisions](04-design-decisions.md) | Why this shape, and the alternatives that were rejected |
| 5 | [Invariant reference](05-invariants.md) | All eleven checks, what each catches, when it fires |
| 6 | [Code walkthrough](06-code-walkthrough.md) | Module by module, following one run through the code |
| 7 | [Usage](07-usage.md) | Installing, writing contracts, running, CI |
| 8 | [Operations](08-operations.md) | Permissions, cost, scheduling, troubleshooting |
| 9 | [Roadmap](09-roadmap.md) | What is deliberately absent, and what comes next |
| 10 | [Deployment and showcase](10-deployment.md) | How to demo it publicly, and what each platform is for |

## The one-paragraph version

A governed metric guarantees *consistency*, not *correctness*. Every dashboard
showing the same wrong number consistently is exactly what governance buys you.
Assay generates correctness checks from the metric definitions a team already
has, runs them against the warehouse on a schedule, and reports what is wrong
with numbers those people already trust. It contains no language model, no
query interface, and no dashboard.
