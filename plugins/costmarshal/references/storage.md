# CostMarshal v4.4 Storage

The runtime root defaults to `$COSTMARSHAL_V2_HOME`, then `$CODEX_HOME/costmarshal-v2`, then `~/.codex/costmarshal-v2`.

```text
<runtime>/
  worker-bundles/<project-id>/<attempt-id>/
  worker-worktrees/<project-id>/<attempt-id>/
  projects/<project-id>/
    project.json
    PROTOCOL.md
    state-backend.json
    scheduler/
      session.json
      state.json
      events.jsonl
      relay-cursors.json
      work-graph.json
      repositories.json
      workstreams.json
      production-boundary.json
      provider-metadata.json
      actors/
      mailboxes/
      state.db
    locks/
      claims.json
      project.lock
    tasks/<task-id>/
      task.json
      status.json
      brief.md
      completion-report.md
      attempts/
    reports/
      results.jsonl
      usage.jsonl
      leader-work.jsonl
      artifacts.jsonl
      gate-results.jsonl
      evaluations.jsonl
      retrospectives.jsonl
      policy-candidates.jsonl
      evolution-cycles.jsonl
      leader-snapshots.jsonl
      leader-decisions.jsonl
      knowledge.jsonl
      skill-candidates.jsonl
      teaching-runs.jsonl
      cost-reports.jsonl
      integration-plans.jsonl
      integration-gates.jsonl
      provider-observations.jsonl
    knowledge/
    summaries/
    skill-candidates/
    actor-homes/
    worktrees/
    transcripts/
```

## Sources of truth

Before explicit cutover, the JSON/JSONL files below are the legacy sources of truth. After `migrate-state --apply`, `scheduler/state.db` is authoritative for mutable control documents, append-only ledgers, payload-hashed commands, and leased runtime effects; the JSON/JSONL files become compatibility views rebuilt from the committed transaction.

- `project.json`: provider catalog, routing/budget policy, workspace, and governance binding.
- `task.json`: task and attempt state, route decisions, required capabilities,
  immutable multimodal attachment receipts, execution mode, reservations,
  actual cost, and leader result.
- actor JSON: runtime identity and process metadata.
- `results.jsonl`: immutable leader judgments used by routing history.
- `work-graph.json`: dependency, role, readiness, and accepted-join state.
- `repositories.json`: immutable repository paths, roles, initial Git heads,
  and task-binding identities. Registration never mutates source repositories.
- `workstreams.json`: project-local Workstream dependencies, repository
  membership, concurrency quotas, and optional CNY allocations.
- `artifacts.jsonl`: task receipts plus `costmarshal-project-artifact-v1`
  metadata and `costmarshal-artifact-lineage-v1` provenance.
- `leader-snapshots.jsonl`: immutable `leader-snapshot-v1` views bound to the
  graph, budget, and artifact revisions without loading raw transcripts.
- `gate-results.jsonl`: deterministic acceptance evidence.
- `evaluations.jsonl`: immutable quality, efficiency, reliability, error, and teaching observations.
- `teaching-runs.jsonl`: hash-bound teaching graph nodes tied to exact result
  and passing Gate evidence.
- `cost-reports.jsonl`: observable total-cost snapshots centered on accepted
  Artifacts; unknown monetary observations remain explicit.
- `integration-plans.jsonl`: non-atomic staged integration plans bound to the
  complete Workstream task set, accepted interface Artifacts, exact current
  repository heads, and real rollback commits.
- `integration-gates.jsonl`: Leader decisions over the frozen plan. Only a
  hash-valid passed Gate unlocks a dependent Workstream.
- `provider-observations.jsonl`: immutable, bounded schema/pricing/capability/
  behavior observations. These rows can only restrict routing.
- `scheduler/provider-metadata.json`: expiring human-reviewed provider
  overrides bound to observation IDs and canonical provider-row hashes.
- `retrospectives.jsonl` and `policy-candidates.jsonl`: project summaries and staged, non-activating learning proposals.
- `evolution-cycles.jsonl`: idempotent per-result local learning summaries;
  each row is aggregate-only and cannot call a Provider or activate policy.
- `usage.jsonl`: immutable usage deltas.
- `scheduler/events.jsonl`: audit events and completed scheduler command IDs.
- `locks/claims.json`: active logical write claims.
- `locks/project.lock`: OS advisory single-writer gate.

`scheduler/production-boundary.json` is optional. It stores only secret-free
external Broker/Proxy endpoints, workload identity, hard-budget posture, and
Artifact IDs for external evidence. It never stores a provider credential.

`status.json` is a materialized task status view and must match `task.json` under `validate`.

## Project artifact boundary

CostMarshal manages only metadata for the current CostMarshal project. A local
small file stays at its source path and is registered with size and SHA-256;
`validate` fails closed if it drifts. Files larger than the local threshold are
external-reference-only and require a non-secret URI, exact size, and SHA-256.
URIs containing credentials, query strings, or fragments are rejected.

Summaries, milestone records, and Skill candidates are new artifacts with
non-empty `derived_from` lineage. `YYYY/MM/DD_name` is a logical query bucket,
not a request to duplicate or reorganize source files. Skill candidates remain
inside the project runtime; CostMarshal does not export or install global
Skills.

Knowledge records are indexes, not a second source tree. They may cite only an
accepted Artifact or a hash-bound explicit Leader decision. A Skill Candidate
requires evidence from at least two distinct accepted tasks. Explicit export
materializes `SKILL.md` and `evidence.json` under the current project's
`skill-candidates/` directory; CostMarshal never writes that export into a
global Skill directory.

## Compatibility

Projects created before provider catalogs load a legacy LongCat/Codex low/high catalog. New projects always persist a validated low/medium/high catalog. Malformed explicit catalogs never fall back silently.

## Crash behavior

Legacy JSON replacements are atomic and JSONL rows are append-only, but a command that updates several legacy files is not one database transaction. Idempotency keys and attempt fencing make replay safer; recovery and validation remain required after an unclean stop.

After SQLite cutover, command state, control documents, ledger rows, view-dirty markers, and runtime-effect intent commit transactionally. Runtime effects are leased and observed outside the transaction, renew the same owner-bound lease during slow OS/OCI I/O, then apply atomically with command completion. Compatibility materialization is serialized across processes so a reused dirty-view revision cannot acknowledge a newer write; atomic replacement retries transient Windows sharing conflicts for a bounded interval, while persistent permission failures leave the revision dirty and fail closed. Recovery re-leases effects only after renewal stops and the lease expires, validates actor/attempt/process identity, rebuilds dirty views, and imports a bounded trusted actor report when a runner crashes after publishing it.
