# Work Graph and evidence-backed evolution

CostMarshal v3.2 learns from use without giving observations authority to
rewrite active policy.

## Authoritative records

Each project stores five transactional compatibility views:

- `scheduler/work-graph.json` — work-package roles, dependencies, readiness,
  and accepted joins.
- `reports/artifacts.jsonl` — content-addressed artifact lifecycle events.
- `reports/gate-results.jsonl` — deterministic acceptance checks and their
  observed/expected values.
- `reports/evaluations.jsonl` — immutable per-attempt scores, error attribution,
  usage variance, teaching evidence, and counterfactual status.
- `reports/retrospectives.jsonl` and `policy-candidates.jsonl` — project-level
  summaries and reviewed promotion inputs.

After SQLite cutover these files are materialized views over the same atomic
control transaction as task state and leader results. The evaluation always
binds the canonical digest of its source result.

## Model memory

`model-memory` rebuilds aggregates across projects under one CostMarshal runtime
root. Profiles are scoped by:

`provider + model + profile hash + task type + difficulty + work role`

They track sample count, leader acceptance, stricter routing success, six mean
scores, demonstrated capabilities, error taxonomy, confidence, and evidence
IDs. Raw prompts, reports, summaries, transcripts, and artifacts are excluded.

Actual routing accepts cross-project history only after each source project's
result receipts pass the normal evidence audit. The richer evaluation aggregate
cannot bypass safety floors, provider capabilities, immutable profile identity,
price snapshots, budgets, or leader acceptance.

## Scoring

Every attempt produces integer scores from 1 to 5:

1. quality;
2. efficiency (derived from estimated versus actual tokens unless reviewed);
3. instruction following;
4. handoff quality;
5. reliability;
6. routing fit.

Failures also record severity from 0 to 5 and one attribution such as routing,
model capability, instruction, context, tool, environment, dependency,
verification, budget, timeout, or human review. An untried model is always
marked `unobserved`; CostMarshal does not invent counterfactual performance.

## Teaching policy

Automatic teaching is considered when:

- the exact model/task scope has fewer than three observations;
- high-risk work needs independent review;
- at least five observations show routing success below 60%;
- aggregate confidence remains below 50%.

`auto` is advisory so legacy workflows are not silently blocked. Explicit
`review`, `paired`, or `replay` modes are enforced gates and require evidence
before acceptance. A zero task budget reduces paired teaching to review.

## Promotion and rollback

A completed project may create a policy candidate. It is never activated
automatically. The only intended progression is:

`observed → aggregated → candidate → replayed → shadow → canary → active`

Every transition requires explicit review and new evidence. A candidate may be
deprecated at any stage; an active policy may be rolled back. This separation
prevents one noisy project, poisoned artifact, or transient provider failure
from immediately changing production routing.

Use `policy-status` to inspect the latest event for each candidate.
`policy-transition` previews by default and requires `--apply`, a stable command
ID, evidence, and an approver to mutate state. An activated reviewed teaching
floor is consumed by subsequent automatic work packages and becomes an enforced
review gate; no earlier lifecycle state affects execution.
