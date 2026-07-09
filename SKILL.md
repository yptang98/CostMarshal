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

The leader must:
- define goals and acceptance criteria
- split work into bounded tasks
- route tasks to agent actors by risk, difficulty, budget, and evidence
- read structured reports and mailbox messages before raw transcripts
- decide whether to accept, retry, or escalate
- record final evaluation with `record-result`
- record direct implementation-like leader work with `record-leader-work`

The scheduler must stay small. It relays messages, launches/stops actors through
a runtime backend, writes durable state, checks recovery, enforces write claims,
and reports status. It must not plan, implement, review, or summarize raw actor
context.

## Runtime Model

v2 models a project as durable actors:

- `scheduler`: relay, mailbox writer, process supervisor, recovery auditor
- `leader`: persistent project controller
- `agent-*`: task-scoped workers that read only their task brief and mailbox

Actor execution is provided by a pluggable backend:

| Backend | Use |
| --- | --- |
| `auto` | Default; Windows uses `local`, macOS/Linux use `tmux` when available and otherwise `local` |
| `local` | Detached local process with pid/log tracking; best for Windows and CI |
| `tmux` | One actor per tmux window; best for Unix servers with tmux |

## Official CLI

```bash
python scripts/costmarshal.py init --name demo --objective "Try scheduler-first orchestration" --backend auto
python scripts/costmarshal.py start-leader --project <project-id> --command "codex --prompt {prompt_file}" --dry-run
python scripts/costmarshal.py new-task --project <project-id> --title "Inspect baseline" --purpose "Return a bounded report" --claim-path reports/baseline.md
python scripts/costmarshal.py dispatch --project <project-id> --task V2-0001 --model gpt-5 --command "codex --model {model} --prompt {prompt_file}"
python scripts/costmarshal.py send --project <project-id> --to leader --message "Task V2-0001 is dispatched."
python scripts/costmarshal.py relay --project <project-id> --actor leader
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

1. Create or select a v2 project with `init`.
2. Start the leader or refresh its dry-run launch plan with `start-leader`.
3. Create bounded tasks with `new-task`; include `--claim-path` for write scopes.
4. Dispatch at most a small number of active agents at once.
5. Give agents only the durable prompt file, task brief, allowed context, and mailbox messages.
6. Use `collect` to move task reports into leader review.
7. Use `record-result` after every worker attempt; worker usage is not leader acceptance.
8. Use `record-leader-work` whenever the leader directly writes or fixes implementation-like work.
9. Use `status` and `validate` as the normal audit surface.
10. Use `recover --plan-restarts` or `recover --restart-missing` after disconnects.

## Isolation Rules

- Treat raw transcripts as audit evidence, not default leader memory.
- Keep worker write scopes disjoint with `--claim-path`; validate active lock conflicts.
- Never put API keys or secrets in prompts, reports, logs, or skill files.
- Use mailbox relay rather than having the scheduler inspect actor reasoning.
- If a worker needs broader context, new write scope, secrets, or architectural judgment, escalate.

## Required Verification

For changes to CostMarshal itself, run:

```bash
python v2/tests/unit_test.py
python v2/tests/smoke_test.py
python v2/tests/local_backend_contract_test.py
python v2/tests/tmux_contract_test.py
python scripts/install_smoke_test.py
```

Compile check:

```powershell
$files = @('scripts/costmarshal.py','v2/run.py') + (Get-ChildItem -Path 'v2/costmarshal_v2','v2/tests','scripts' -Filter '*.py' | ForEach-Object { $_.FullName })
python -m py_compile @files
```
