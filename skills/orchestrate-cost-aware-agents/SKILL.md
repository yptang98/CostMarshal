---
name: orchestrate-cost-aware-agents
description: Orchestrate Codex work across low-, medium-, and high-cost API providers with CostMarshal's Work Graph, model memory, teaching policy, safety floors, leader acceptance, artifact gates, budget reservations, recoverable execution, and optional read-only ArchMarshal governance. Use when a user asks Codex to optimize cost versus quality, coordinate multiple model/API tiers, operate or audit CostMarshal, resume a CostMarshal run, or complete a task through economical provider handoffs.
---

# Orchestrate cost-aware agents

Treat this as the Codex-native product entry point. Keep Python and CLI commands
as internal implementation and diagnostic details unless the user explicitly
asks for them.

Before taking task actions, read [`../../SKILL.md`](../../SKILL.md) completely
and follow its scheduler, safety, evidence, recovery, budget, and ArchMarshal
compatibility requirements. Resolve every relative script or reference path
from the plugin root, two directories above this file.

Interact with the user in Codex: translate natural-language intent into the
bounded CostMarshal workflow, report durable outcomes, and expose a manual
command only when recovery or diagnosis genuinely requires it.

## Natural-language control plane

Classify the user's request before running the internal engine:

- **Set up CostMarshal**: discover an existing CostMarshal project first,
  including compatible v2 state. If none exists,
  obtain only the missing objective, writable workspace, reviewed low/medium/high
  provider catalog, budget, and governance choice. Preview provider/profile
  changes before applying them, never request secret values in chat, then run
  `init` with a stable project name.
- **Plan or explain**: use `providers`, `budget`, and read-only `route`. Explain
  the safety floor, complete admitted chain, per-step reservation, historical
  acceptance evidence, and why a cheaper route was rejected. Do not create a
  task or start a provider.
- **Do new work**: inspect `status`, `work-graph`, and relevant `model-memory`
  scopes. Decompose the request into bounded work packages with roles,
  dependencies, deliverables, gates, and teaching policy. Create each package
  with `new-task`, run a read-only route explanation, then `dispatch --start`
  only when its dependencies are accepted and it remains within the user's
  workspace, budget, paths, capabilities, and acceptance criteria. Start the
  scheduler in bounded cycles and stop monitoring only at gated leader
  acceptance, explicit failure, budget exhaustion, a recoverable pause, or user
  stop.
- **Audit or monitor**: use JSON `status`, `dashboard`, `work-graph`,
  `model-memory`, `providers`, `budget`, `validate`, and read-only
  `governance-status`. Summarize dependency readiness, gates, evaluations,
  errors, cost, and any unpromoted policy candidate; never infer success only
  from a live process.
- **Resume or recover**: run `recover` read-only first. Show the exact restart
  plan before `--restart-missing`; preserve sealed routes, generations, attempts,
  reservations, runtime receipts, and leader ownership. Never silently respawn
  an uncertain actor.
- **Stop**: use the actor's durable identity with `stop-actor --stop-runtime`,
  then verify terminal state and cleanup receipts. Do not kill by an unverified
  PID or process name.

## Internal execution contract

Resolve the interpreter without changing global configuration: prefer an active
Python 3.11+ executable; on Windows fall back to `py -3.11`. Invoke
`scripts/costmarshal.py --root <runtime-root> ...` from the plugin root. Prefer
JSON output, parse it structurally, and treat a nonzero exit, malformed JSON, or
an invariant/identity/evidence error as a stopped operation rather than a reason
to improvise state edits.

Use idempotency keys for retried mutating commands. Never edit runtime JSON,
SQLite state, attempt records, price/profile evidence, receipts, or sealed route
envelopes by hand. Do not run an unbounded foreground watch; advance the
scheduler with bounded cycles, report progress in Codex, and re-read durable
state between cycles.

At leader review, record quality, efficiency, instruction following, handoff
quality, and error attribution. Supply explicit teaching evidence when the work
package requires review, pairing, or replay. Treat model outcomes not actually
run as unobserved. A project retrospective may propose a candidate, but never
promote it directly to active routing; require reviewed replay, shadow, and
canary evidence first.

The plugin Skill is the only implicit CostMarshal entry. A separately installed
legacy `$costmarshal` Skill is explicit-only and may coexist solely for migration
or diagnostics; it must not own natural-language routing.
