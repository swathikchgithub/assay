---
title: Assay Playground
emoji: 🔬
colorFrom: blue
colorTo: gray
sdk: gradio
sdk_version: 6.26.0
app_file: app.py
pinned: false
license: mit
short_description: Toggle a metric definition, watch the checks
---

# Assay playground

A synthetic warehouse with seven planted defects, and six metric definitions
describing it. **Every control edits a definition, never the data.**

The checks are generated from the contract, so changing what a metric claims
about itself changes which checks exist at all. Flip `active_users` to
additive and a 1477% discrepancy appears from a single label — nothing is
wrong with the data or the SQL.

Then press **Restate a closed month** to reproduce the finding almost nothing
else reports: a month everyone considered finished, silently rewritten.

Assay is phase P0 — an invariant engine over metric definitions a team
already has. No language model, no query interface, no dashboard.
