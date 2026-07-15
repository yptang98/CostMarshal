# CostMarshal v2.3 Storage

The runtime root defaults to `$COSTMARSHAL_V2_HOME`, then `$CODEX_HOME/costmarshal-v2`, then `~/.codex/costmarshal-v2`.

```text
<runtime>/projects/<project-id>/
  project.json
  session.json
  protocol.md
  scheduler/
    scheduler.json
    relay-cursors.json
    locks.json
    actors/
    mailboxes/
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
  events.jsonl
```

## Sources of truth

- `project.json`: provider catalog, routing/budget policy, workspace, and governance binding.
- `task.json`: task and attempt state, route decisions, reservations, actual cost, and leader result.
- actor JSON: runtime identity and process metadata.
- `results.jsonl`: immutable leader judgments used by routing history.
- `usage.jsonl`: immutable usage deltas.
- `events.jsonl`: audit events and completed scheduler command IDs.
- `locks.json`: active logical write claims.
- `locks/project.lock`: OS advisory single-writer gate.

`status.json` is a materialized task status view and must match `task.json` under `validate`.

## Compatibility

Projects created before provider catalogs load a legacy LongCat/Codex low/high catalog. New projects always persist a validated low/medium/high catalog. Malformed explicit catalogs never fall back silently.

## Crash behavior

Individual JSON replacements are atomic and JSONL rows are append-only, but a command that updates several files is not one database transaction. Idempotency keys and attempt fencing make replay safer; recovery and validation remain required after an unclean stop. A future non-beta state version should use a transactional store with unique command IDs and a transactional outbox.
