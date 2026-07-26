# Work Graph and evidence-backed evolution

CostMarshal v3.5 learns from use without giving observations authority to
rewrite active policy.

## Authoritative records

Each project stores transactional compatibility views:

- `scheduler/work-graph.json` — work-package roles, dependencies, readiness,
  and accepted joins.
- `reports/artifacts.jsonl` — content-addressed Artifact lifecycle events.
- `reports/gate-results.jsonl` — deterministic acceptance checks.
- `reports/evaluations.jsonl` — immutable per-attempt scores, attribution,
  usage variance, and counterfactual status.
- `reports/teaching-runs.jsonl` — exact teaching graph/result/Gate bindings.
- `reports/cost-reports.jsonl` — evidence-bound total-cost snapshots.
- `reports/retrospectives.jsonl` and `policy-candidates.jsonl` — project-level
  summaries and reviewed promotion inputs.

After SQLite cutover, these files are materialized views over the same atomic
control transactions as task state and Leader results.

## Model memory

`model-memory` rebuilds aggregate profiles across projects under one runtime
root. Every profile is isolated by:

`provider + model + profile hash + task type + difficulty + work role`

Model/version changes therefore cannot silently inherit old evidence. Profiles
publish raw sample counts, effective sample counts with a 90-day half-life,
six raw and recency-weighted mean scores, 95% Wilson intervals for acceptance
and routing success, demonstrated capabilities, error taxonomy, and exact
evaluation IDs. Raw prompts, reports, summaries, transcripts, and Artifacts
are excluded.

Routing consumes audited exact Leader-result evidence rather than trusting this
disposable aggregate. Negative outcomes attributed to routing, context, tools,
environment, dependencies, budget, or human review do not penalize model
capability. They remain in the evaluation and total-cost ledgers. Model,
instruction, timeout, verification, and unknown failures remain conservative
routing evidence.

## Scoring

Every attempt produces integer scores from 1 to 5 for quality, efficiency,
instruction following, handoff, reliability, and routing fit. Failures record
severity from 0 to 5 and one explicit attribution. An untried model is always
`unobserved`; CostMarshal does not invent counterfactual performance.

## Teaching execution graphs

Teaching is considered for cold-start scopes, high-risk work, repeated weak
outcomes, and low-confidence evidence. `auto` remains advisory. Explicit
`review`, `paired`, or `replay` modes are enforced.

Every non-off mode stores a task-bound, hash-bound graph:

- `review`: candidate result → independent reviewer result → passing Gate.
- `paired`: two distinct execution identities → reviewer comparison → passing
  Gate.
- `replay`: baseline result → same-identity/same-scope replay → fixed passing
  Gate.

`record-teaching-run` binds every graph node to exact result/Gate hashes.
Enforced Leader acceptance uses `record-result --teaching-run <run-id>`;
arbitrary free-form strings are rejected for new structured-graph tasks.
Historical tasks without a graph remain readable.

## Total-cost model

`cost-report` records known monetary cost per accepted Artifact and six
observable dimensions:

1. worker execution;
2. independent verification;
3. retries, attempts, and escalation;
4. structured handoff/context transfer;
5. Leader attention;
6. failure, recovery, and Gate risk.

Terminal result receipts are authoritative for completed attempts. Unmatched
in-progress usage is included without double-counting terminal attempts.
Unknown monetary observations are counted explicitly. Tokens, wall time,
handoff bytes, and Leader minutes are never assigned an invented CNY price, so
the headline metric is `complete`, `partial`, or
`unavailable-no-accepted-artifacts`.

## Promotion and rollback

A completed project may create a policy candidate. It is never activated
automatically. The intended progression is:

`observed → aggregated → candidate → replayed → shadow → canary → active`

Every transition requires explicit review and evidence. A candidate may be
deprecated at any stage; an active policy may be rolled back. Teaching runs,
model memory, and cost reports are evidence only and cannot bypass this
lifecycle.
