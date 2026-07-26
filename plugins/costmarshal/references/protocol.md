# CostMarshal v3.5 Protocol

This is the canonical v2 protocol. Legacy `scripts/mc.py` commands are not part of it.

## Roles

- `scheduler`: deterministic relay, locking, supervision, accounting, and recovery.
- `leader`: planning, task boundaries, review, integration, and final acceptance.
- `agent-*`: one bounded provider attempt with explicit tier, context, and write scope.

Agent work packages use the roles `scout`, `builder`, `reviewer`, and `expert`.
The Work Graph is authoritative for dependency readiness. A package cannot
dispatch until every predecessor is leader-accepted.

## Task lifecycle

```text
planned -> dispatched -> waiting_leader -> done
                    \-> failed
                    \-> escalate -> exact admitted successor
```

Only `record-result --status done --accepted-by-leader` may create `done`. A worker collect request is limited to `waiting_leader`, `failed`, or `escalate`.

Leader acceptance is also subject to configured gates: dependencies, minimum
quality, maximum error severity, required artifact kinds, and enforced teaching
evidence. Each result creates an artifact receipt, gate record, and attempt
evaluation in the same control transaction.

For new tasks with `review`, `paired`, or `replay`, teaching evidence is a
hash-bound execution graph rather than an arbitrary note. Record the completed
graph with `record-teaching-run`, then pass its ID through
`record-result --teaching-run`. Review requires an independent reviewer work
package; paired mode requires distinct execution identities plus a reviewer
comparison; replay fixes execution identity and task scope. `auto` remains
advisory unless a separately reviewed active policy raises the teaching floor.

`cost-report` snapshots execution, verification, rework, handoff/context,
Leader attention, and failure/recovery evidence. Completed attempt costs come
from terminal result receipts; unmatched in-progress usage is included once.
The primary metric is known CNY per accepted Artifact and is explicitly marked
partial or unavailable when evidence is incomplete.

Leader review can use `leader-snapshot-v1`, a deterministic projection of the
objective, Work Graph readiness/blocking, pending decisions, recent accepted or
rejected artifacts, risks, and budget state. It is bound to graph, budget, and
artifact revisions and never embeds the raw transcript.

`batch-acceptance` evaluates each task through its normal independent Gates
inside one payload-hashed SQLite transaction. One invalid decision rolls back
the complete batch, so partial acceptance cannot leak into later decisions.

## Attempt fencing

Every attempt has a unique `attempt_id`, actor ID, provider ID, tier, and launch token. The runner holds an attempt-specific lifetime lock, revalidates the current task/attempt/actor binding, and registers a process start marker before invoking a provider. A duplicate runner or an attempt whose prior execution outcome is unknown must not invoke the provider.

Mailbox message IDs are idempotency keys. Replaying task creation, dispatch, escalation, collection, usage, or result commands must not duplicate their durable effect.

New projects write `structured-handoff-v2`: one conclusion, bounded facts,
typed Artifact/path/Gate evidence, unresolved issues, and next actions. The
capsule preserves the existing byte/token reserves and immutable result/output
bindings. Legacy text handoffs remain read-compatible but are not writable by
new projects.

## Project knowledge and reuse

Project knowledge has an accepted-only promotion boundary. Charter,
Architecture, ADR, Interface, Accepted Fact, Risk, and Milestone Summary
records must cite accepted Artifact IDs or an explicit hash-bound Leader
decision. Rejected attempts never become knowledge automatically.

Project and milestone summaries are deterministic lineage manifests. Date paths
such as `YYYY/MM/DD_name` are logical filters and never duplicate source files.
A Skill Candidate requires accepted evidence from two distinct tasks and
records applicability, inputs, steps, verification, and failure boundaries.
Candidate export is explicit, project-local, and separate from global Skill
validation or installation.

## Routing

Safety establishes a minimum tier. Complete, reviewed price and token inputs enable bounded provider-chain optimization. A mature plan contains one to three unique provider IDs with non-decreasing tiers; cold-start bootstrap remains one provider per available tier. New projects use completion-first and retain a strongest-compatible terminal fallback; explicit cost-only and legacy projects may terminate earlier. Every successor still requires leader rejection. Incomplete economic inputs fall back to the minimum safe available tier.

Required capabilities are hard constraints evaluated before price or acceptance
history. Built-in provider presets publish only the intersection of documented
API capability and worker transport capability. `--input-image` accepts a
committed workspace-relative image, adds it to allowed context, and adds
`input:image`; the scheduler rejects text-only routes before provider launch.
Audio/video/document API support is informational until the worker protocol can
bind and transport that modality end to end.

Each route step binds its own ordinary/cached/output forecast. Cached input is portable only with a proven exact provider/model/profile/profile-hash origin; a missing origin or different successor identity reclassifies it as ordinary input. Route-plan v2, budget-envelope v3, and collaboration-contract v2 bind this forecast. Missing usage cannot settle a reservation, while an explicit all-zero final observation can settle the immutable per-attempt fixed fee.

Escalation follows the exact next provider in an active sealed envelope. That provider may be a distinct same-tier peer or a stronger tier, and a two-tier legacy catalog may skip a missing medium tier. Without such a sealed same-tier step, escalation remains stronger-tier only; repeats and downgrades are forbidden.

New routing evidence distinguishes leader acceptance from quality-aware routing
success. An accepted result trains the economic prior as a success only when its
gates pass, quality is at least three, and error severity is at most one.
Audited sibling-project evidence under the same runtime root may contribute to
the prior; invalid peer evidence is skipped and never weakens the active
project's audit.

## Evolution and teaching

Attempt evaluations score quality, efficiency, instruction following, handoff,
reliability, and routing fit from one to five, with explicit error attribution.
Cross-project model memory is rebuilt from these ledgers and stores aggregates
only. It cannot override safety, capability, price, budget, profile, isolation,
or leader gates.

Automatic teaching is advisory for cold starts, risk, weak repeated outcomes,
and low-confidence scopes. Explicit `review`, `paired`, or `replay` teaching
requires evidence before acceptance. Policy recommendations begin as
non-activating candidates and require reviewed replay, shadow, and canary stages
before activation.

### Pricing snapshot gate

Non-beta pricing is a nested, immutable snapshot containing currency, source,
review/effective/expiry timestamps, snapshot ID, canonical SHA-256 hash, normal
input, cached input, output, and fixed-attempt rates. Routing evaluates
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

The v3 OCI contract validates mounts, rootfs, UID, capabilities, no-new-privileges, engine locality, image digest, resources, and canary output before state or budget reservation. Required execution uses a bounded JSONL worker adapter with one selected credential, a sanitized profile, stdin-only prompts, a strict output exchange, immutable container identity, and cleanup receipts. Unrestricted bridge networking is forbidden. A `provider-proxy` network must be engine-attested as internal, carry the CostMarshal trust label, and be paired with a separately reviewed dual-homed proxy for controlled egress.

The selected provider client and credential share one trust domain inside the
container. OCI boundaries keep that worker away from host files and other
provider keys, but cannot stop a malicious in-container workload from encoding
the selected key. Redaction is accidental-disclosure defense only. Hostile
workloads require an out-of-process credential broker issuing scoped
attempt/provider/budget/time capabilities; v3.0 does not include that broker.

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

Pure commands commit their state, ledger, event, and mailbox view mutations together under a payload-hashed command ID. Dirty compatibility views are rebuilt after a post-commit crash. OS spawn/stop uses a leased transactional effect outbox: intent and command state commit first, external I/O occurs outside the database transaction, observation is durably recorded, and final materialization atomically applies the effect and completes the command. Attempt lifetime locks, launch tokens, process/container identity, lease expiry, and actor report recovery prevent a replay from silently duplicating provider work.
