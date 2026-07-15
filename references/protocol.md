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

Every attempt has a unique `attempt_id`, actor ID, provider ID, tier, and launch token. The runner holds an attempt-specific lifetime lock, revalidates the current task/attempt/actor binding, and registers a process start marker before invoking a provider. A duplicate runner or an attempt whose prior execution outcome is unknown must not invoke the provider.

Mailbox message IDs are idempotency keys. Replaying task creation, dispatch, escalation, collection, usage, or result commands must not duplicate their durable effect.

## Routing

Safety establishes a minimum tier. Complete, reviewed price and token inputs enable cross-tier chain optimization. Incomplete economic inputs fall back to the minimum safe available tier.

Escalation selects the next enabled stronger tier; a two-tier legacy catalog may skip a missing medium tier.

### Pricing snapshot gate

Non-beta pricing is a nested, immutable snapshot containing currency, source,
review/effective/expiry timestamps, snapshot ID, canonical SHA-256 hash, normal
input, cached input, output, and fixed-request rates. Routing evaluates
freshness against an injected or UTC clock. A future-reviewed, future-effective,
expired, mixed-currency, mixed canonical/legacy, hash-mismatched, or unsupported
snapshot cannot emit a CNY estimate; unbudgeted routing degrades explicitly to
the safe tier, while budget enforcement fails closed on the missing estimate.

Flat `input_cny_per_1m` and `output_cny_per_1m` values are beta legacy
compatibility only. They remain readable for existing v2.3 projects, but do not
carry provenance, freshness, cached-input pricing, fixed fees, or an immutable
snapshot.

## Execution isolation

Production worker dispatch uses `required` isolation and may select only an attested local Docker/Podman Linux engine with a digest-pinned image. Native execution is never a fallback. Unsafe native development execution requires two explicit opt-ins and records a weak attestation. Required governance forbids it.

The beta OCI contract validates mounts, rootfs, UID, capabilities, no-new-privileges, engine locality, image digest, resources, and canary output before state or budget reservation. Required execution remains fail-closed until the container snapshot/profile/credential/report exchange adapter is enabled.

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

## Transaction and recovery boundary

Legacy projects use atomic JSON/JSONL until an explicit offline `migrate-state --apply`. Cutover creates a full backup and staged SQLite database, validates integrity and foreign keys, installs the database, then writes `state-backend.json` last. After the marker, SQLite WAL is authoritative for mutable JSON/JSONL control views; a missing or malformed enabled database fails closed.

Pure commands commit their state, ledger, event, and mailbox view mutations together under a payload-hashed command ID. Dirty compatibility views are rebuilt after a post-commit crash. OS spawn/stop remains a separate effect boundary; those commands are blocked on SQLite-backed projects until the leased effect outbox and runner self-registration path are complete.
