---
name: costmarshal
description: "CostMarshal v2: scheduler-first, cost-aware multi-agent orchestration for Codex CLI with durable actor mailboxes, pluggable runtime backends, leader acceptance records, write locks, recovery, and audit trails."
---

# CostMarshal v2

CostMarshal v2 is the official implementation line. Use `scripts/costmarshal.py`
for durable state and orchestration. The legacy v1 engine remains in
`scripts/mc.py` only for historical reference; do not use it for new runs.

## Core Contract

Keep the leader as the project controller, not the bulk implementer.

The on-demand Codex manager must:
- define goals and acceptance criteria
- split work into bounded tasks
- route tasks to agent actors by risk, difficulty, budget, and evidence
- read structured reports and mailbox messages before raw transcripts
- decide whether to accept, retry, or escalate
- record final evaluation with `record-result`
- record direct implementation-like leader work with `record-leader-work`

The scheduler must stay small. It relays messages, launches/stops actors through
a runtime backend, executes validated actor-authored scheduler commands, writes
durable state, checks recovery, enforces write claims, records usage, and
reports status. It must not plan, implement, review, or summarize raw actor
context.

## Runtime Model

v2 models a project as durable actors:

- `scheduler`: relay, mailbox writer, process supervisor, recovery auditor
- `leader`: on-demand Codex project controller invoked at planning/review/integration gates
- `agent-*`: task-scoped Codex or LongCat workers that receive a bounded prompt and return one final report

Actor execution is provided by a pluggable backend:

| Backend | Use |
| --- | --- |
| `auto` | Default; Windows uses `local`, macOS/Linux use `tmux` when available and otherwise `local` |
| `local` | Detached local process with pid/log tracking; best for Windows and CI |
| `tmux` | One actor per tmux window; best for Unix servers with tmux |

## Official CLI

```bash
python scripts/costmarshal.py configure-profiles
python scripts/costmarshal.py init --name demo --objective "Try cost-aware rotation" --workspace . --backend auto
python scripts/costmarshal.py run-scheduler --project <project-id> --interval 2
python scripts/costmarshal.py dashboard --project <project-id> --watch
python scripts/costmarshal.py new-task --project <project-id> --title "Inspect baseline" --purpose "Return a bounded report" --risk low --provider auto --claim-path reports/baseline.md
python scripts/costmarshal.py dispatch --project <project-id> --task V2-0001 --start
python scripts/costmarshal.py escalate --project <project-id> --task V2-0001 --reason "Needs stronger judgment" --start
python scripts/costmarshal.py run-manager --project <project-id>
python scripts/costmarshal.py send --project <project-id> --to leader --message "Task V2-0001 is dispatched."
python scripts/costmarshal.py relay --project <project-id> --actor leader
python scripts/costmarshal.py record-usage --project <project-id> --actor agent-v2-0001 --input-tokens 100 --output-tokens 40
python scripts/costmarshal.py collect --project <project-id> --task V2-0001 --state waiting_leader
python scripts/costmarshal.py record-result --project <project-id> --task V2-0001 --status done --quality-score 4 --accepted-by-leader
python scripts/costmarshal.py record-leader-work --project <project-id> --task V2-0001 --work-type verification --risk low --scope "Sampled evidence" --reason "Leader acceptance requires review"
python scripts/costmarshal.py stop-actor --project <project-id> --actor agent-v2-0001 --reason "task complete"
python scripts/costmarshal.py status --project <project-id>
python scripts/costmarshal.py recover --project <project-id> --plan-restarts
python scripts/costmarshal.py validate --project <project-id>
```

Use `--root <dir>` or `COSTMARSHAL_V2_HOME` to choose the runtime root. Default
storage is `$CODEX_HOME/costmarshal-v2` when `CODEX_HOME` is set, otherwise
`~/.codex/costmarshal-v2`.

## Dispatch Discipline

1. Create the user-level LongCat profile once with `configure-profiles`; provide `LONGCAT_API_KEY` through the environment or a local secrets file.
2. Create or select a v2 project with `init --workspace <dir>`.
3. Create bounded tasks with `new-task`; include `--claim-path` for write scopes.
4. Dispatch at most a small number of active agents at once. Auto routing uses LongCat for bounded low-risk work and Codex for high-risk/hard work.
5. Give agents only the durable prompt file, task brief, allowed context, and mailbox messages.
6. Keep `run-scheduler` active so LongCat failures or `Status: escalate` reports launch a fresh Codex attempt.
7. Use `dashboard --watch` to monitor scheduler, leader, agents, process liveness, mailbox counts, logs, and agent token totals.
8. Use `collect` to move task reports into leader review when manually operating without the scheduler loop.
9. Use `record-result` after every worker attempt; worker usage is not leader acceptance.
10. Use `record-leader-work` whenever the leader directly writes or fixes implementation-like work.
11. Use `status`, `dashboard`, and `validate` as the normal audit surface.
12. Invoke `run-manager` only for planning, review, integration, rescue, or final acceptance gates.
13. Use `recover --plan-restarts` or `recover --restart-missing` after disconnects.

## Isolation Rules

- Treat raw transcripts as audit evidence, not default leader memory.
- Keep worker write scopes disjoint with `--claim-path`; validate active lock conflicts.
- Never put API keys or secrets in prompts, reports, logs, or skill files.
- Use mailbox relay rather than having the scheduler inspect actor reasoning.
- The default actor runner persists reports, usage, and scheduler commands; models do not edit runtime state themselves.
- Custom/legacy actors may still append scheduler commands to outbox messages addressed to `scheduler`.
  Supported commands are `create_task`, `dispatch_task`, `escalate_task`, `collect_task`,
  `record_result`, `record_usage`, `heartbeat`, and `stop_actor`.
- If a worker needs broader context, new write scope, secrets, or architectural judgment, escalate.

## Required Verification

For changes to CostMarshal itself, run:

```bash
python tests/unit_test.py
python tests/smoke_test.py
python tests/local_backend_contract_test.py
python tests/tmux_contract_test.py
python tests/model_rotation_contract_test.py
python scripts/install_smoke_test.py
```

Compile check:

```powershell
$files = @('scripts/costmarshal.py') + (Get-ChildItem -Path 'costmarshal_v2','tests','scripts' -Filter '*.py' | ForEach-Object { $_.FullName })
python -m py_compile @files
```

