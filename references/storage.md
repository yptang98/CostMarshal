# CostMarshal v2.4 Storage

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
    actor-homes/
    worktrees/
    transcripts/
```

## Sources of truth

Before explicit cutover, the JSON/JSONL files below are the legacy sources of truth. After `migrate-state --apply`, `scheduler/state.db` is authoritative for mutable control documents, append-only ledgers, payload-hashed commands, and leased runtime effects; the JSON/JSONL files become compatibility views rebuilt from the committed transaction.

- `project.json`: provider catalog, routing/budget policy, workspace, and governance binding.
- `task.json`: task and attempt state, route decisions, reservations, actual cost, and leader result.
- actor JSON: runtime identity and process metadata.
- `results.jsonl`: immutable leader judgments used by routing history.
- `usage.jsonl`: immutable usage deltas.
- `scheduler/events.jsonl`: audit events and completed scheduler command IDs.
- `locks/claims.json`: active logical write claims.
- `locks/project.lock`: OS advisory single-writer gate.

`status.json` is a materialized task status view and must match `task.json` under `validate`.

## Compatibility

Projects created before provider catalogs load a legacy LongCat/Codex low/high catalog. New projects always persist a validated low/medium/high catalog. Malformed explicit catalogs never fall back silently.

## Crash behavior

Legacy JSON replacements are atomic and JSONL rows are append-only, but a command that updates several legacy files is not one database transaction. Idempotency keys and attempt fencing make replay safer; recovery and validation remain required after an unclean stop.

After SQLite cutover, command state, control documents, ledger rows, view-dirty markers, and runtime-effect intent commit transactionally. Runtime effects are leased and observed outside the transaction, then applied atomically with command completion. Recovery re-leases expired effects, validates actor/attempt/process identity, rebuilds dirty views, and imports a bounded trusted actor report when a runner crashes after publishing it.
