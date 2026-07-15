# CostMarshal v2.3 Protocol

This is the canonical v2 protocol. Legacy `scripts/mc.py` commands are not part of it.

## Roles

- `scheduler`: deterministic relay, locking, supervision, accounting, and recovery.
- `leader`: planning, task boundaries, review, integration, and final acceptance.
- `agent-*`: one bounded provider attempt with explicit tier, context, and write scope.

## Task lifecycle

```text
planned -> dispatched -> waiting_leader -> done
                    \-> failed
                    \-> escalate -> next stronger attempt
```

Only `record-result --status done --accepted-by-leader` may create `done`. A worker collect request is limited to `waiting_leader`, `failed`, or `escalate`.

## Attempt fencing

Every attempt has a unique `attempt_id`, actor ID, provider ID, and tier. Worker usage, collect, and escalation commands carry actor and attempt identity. A command from an older attempt is rejected after a stronger attempt exists.

Mailbox message IDs are idempotency keys. Replaying task creation, dispatch, escalation, collection, usage, or result commands must not duplicate their durable effect.

## Routing

Safety establishes a minimum tier. Complete, reviewed price and token inputs enable cross-tier chain optimization. Incomplete economic inputs fall back to the minimum safe available tier.

Escalation selects the next enabled stronger tier; a two-tier legacy catalog may skip a missing medium tier.

## Write isolation

Claims coordinate leaders; worktrees enforce workers.

- Report-only tasks use a read-only source workspace.
- Writable tasks require a clean git repository and execute in a detached worktree.
- The post-run diff must be contained by allowed/claimed paths.
- Runtime, governance, git metadata, project Codex configuration, and plugin paths are protected.

## Secrets

Low/medium actors receive a minimal environment, one provider key, and a per-actor Codex home containing only the selected profile. The runner removes the secrets-file path and redacts all parsed provider values from stdout and reports.

## Budget

A priced attempt reserves its estimated cost at dispatch. Project commitment is actual settled cost plus the remaining active/unsettled reservation. Usage accumulates actual cost; leader results settle attempts. Dispatch fails before mutation when the task or project commitment would exceed its limit.

## Governance

ArchMarshal checks are explicit and read-only. Required binding drift blocks dispatch, launch, and recovery restart. CostMarshal never performs ArchMarshal lifecycle mutations.

## Recovery boundary

The v2.3-beta state backend is multiple atomic JSON files plus append-only JSONL ledgers behind an OS writer lock. It is concurrency-safe for covered writers but not fully crash-atomic across a multi-file dispatch or escalation. Run `validate` and `recover` after hard termination.
