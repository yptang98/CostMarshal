# CostMarshal v2 Beta

CostMarshal v2 is a separate scheduler-first implementation. It does not
replace v1 and does not mutate v1 project directories. You can reference an
existing v1 project as read-only input with `--source-project`.

The v2 runtime models the system as actors:

- scheduler: relay, process supervisor, mailbox writer, recovery auditor
- leader: persistent project controller
- agents: task-scoped workers that can be started, stopped, or left idle

Actor execution is provided by a pluggable session backend. `auto` chooses a
platform-appropriate backend: Windows defaults to `local`, while macOS/Linux
use `tmux` when it is available and otherwise fall back to `local`. You can
force a backend with `--backend tmux` or `--backend local`.

The scheduler writes durable state under its own runtime root:

```text
<COSTMARSHAL_V2_HOME or ~/.codex/costmarshal-v2>/
  projects/<project-id>/
    project.json
    PROTOCOL.md
    scheduler/
      session.json
      events.jsonl
      actors/
      mailboxes/
    tasks/
    reports/
      results.jsonl
      leader-work.jsonl
    transcripts/
```

## Quick Start

```bash
python v2/run.py init --name demo --objective "Try scheduler-first orchestration" --backend auto
python v2/run.py start-leader --project <project-id> --command "codex --prompt {prompt_file}" --dry-run
python v2/run.py new-task --project <project-id> --title "Inspect baseline" --purpose "Return a bounded report" --claim-path reports/baseline.md
python v2/run.py dispatch --project <project-id> --task V2-0001 --model gpt-5 --command "codex --model {model} --prompt {prompt_file}"
python v2/run.py send --project <project-id> --to leader --message "Task V2-0001 is dispatched."
python v2/run.py relay --project <project-id> --actor leader
python v2/run.py collect --project <project-id> --task V2-0001 --state waiting_leader
python v2/run.py record-result --project <project-id> --task V2-0001 --status done --quality-score 4 --accepted-by-leader
python v2/run.py record-leader-work --project <project-id> --task V2-0001 --work-type verification --risk low --scope "Sampled evidence" --reason "Leader acceptance requires review"
python v2/run.py stop-actor --project <project-id> --actor agent-v2-0001 --reason "task complete"
python v2/run.py status --project <project-id>
python v2/run.py recover --project <project-id> --plan-restarts
python v2/run.py validate --project <project-id>
```

Use `--start` on `dispatch` or `start-leader` without `--dry-run` to launch
through the configured backend. The `local` backend starts a detached process
and records its pid/log path; the `tmux` backend starts windows in a tmux
session. Use `stop-actor --stop-runtime` to close a backend runtime after its
report is collected, or leave it `idle` through `heartbeat` if you want it on
standby. `recover --restart-missing` can relaunch actors that were marked
running but no longer have a live backend runtime.

## Actor Launch Context

Every actor has a durable prompt file next to its actor state:

```text
scheduler/actors/leader.prompt.md
scheduler/actors/agent-v2-0001.prompt.md
```

The prompt points to the protocol, actor state, mailbox files, and assigned
task brief/report paths. It is the first file to read after reconnecting to a
runtime or restarting an actor.

Command templates can use these placeholders:

| Placeholder | Meaning |
| --- | --- |
| `{project}` | v2 project directory |
| `{project_id}` | v2 project id |
| `{actor}` | actor id |
| `{task}` | assigned task id, if any |
| `{model}` | actor model |
| `{mailbox}` | actor mailbox directory |
| `{prompt_file}` / `{prompt}` | durable actor prompt file |
| `{brief}` | assigned task brief path |
| `{report}` | assigned task completion report path |

`recover` repairs missing mailbox files and missing actor prompt files. It can
also plan or perform backend restarts for actors that were marked running but
lost their runtime.

## Session Backends

Current backends:

| Backend | Intended hosts | Behavior |
| --- | --- | --- |
| `local` | Windows, minimal shells, CI | Starts a detached local process, records pid and transcript log, and uses mailbox files for communication. |
| `tmux` | macOS/Linux servers with tmux | Starts one actor per tmux window and supports optional runtime text injection. |

The scheduler talks to a backend interface, not directly to tmux. Platform
specific implementations should live behind that interface so the durable
project state stays portable.

## Mailbox Relay

The scheduler can send a message directly:

```bash
python v2/run.py send --project <project-id> --to leader --message "Status changed"
```

Actors can also append JSON messages to their own `outbox.jsonl`. The scheduler
then relays those messages with a durable cursor:

```bash
python v2/run.py relay --project <project-id> --actor leader
```

Relay messages must include:

```json
{
  "from": "leader",
  "to": "agent-v2-0001",
  "subject": "Dispatch detail",
  "body": "Use the task brief and report back.",
  "task_id": "V2-0001"
}
```

`relay` advances `scheduler/relay-cursors.json` after processing outbox lines,
skips messages that are already in the target inbox, and records
`message_relayed` events. This keeps the scheduler as a delivery mechanism
rather than a reader of actor context.

## Leader Acceptance And Audit

After a worker report is collected, the leader records the final evaluation:

```bash
python v2/run.py record-result --project <project-id> --task V2-0001 --status done --quality-score 4 --accepted-by-leader --input-tokens 1000 --output-tokens 400 --estimated-cost-cny 0.02 --summary "Accepted after evidence check"
```

The row is appended to `reports/results.jsonl`, copied into the task's latest
`leader_result`, and reflected in `status` as result count, accept rate,
quality, tokens, and known estimated cost. Use `failed` or `escalate` when the
leader rejects or escalates the attempt.

If the leader performs direct implementation-like work instead of delegating it,
record a small audit exception:

```bash
python v2/run.py record-leader-work --project <project-id> --task V2-0001 --work-type verification --risk low --scope "Sampled evidence and final integration check" --reason "Final acceptance cannot be delegated"
```

These rows are stored in `reports/leader-work.jsonl` and summarized by `status`.
`validate` checks that both ledgers reference real tasks and contain sane
quality, risk, token, and cost fields.

## Write Claims

Tasks can claim file or directory paths before they are dispatched:

```bash
python v2/run.py new-task --project <project-id> --title "Write report" --purpose "Produce one report" --claim-path reports/result.md
```

Active claims are stored in `locks/claims.json`. The scheduler rejects a new
task if its `--claim-path` overlaps an active task's claim:

```bash
python v2/run.py new-task --project <project-id> --title "Conflicting report" --purpose "Should wait" --claim-path reports/result.md
```

Claims are released when the owning task reaches a terminal state such as
`done`, `failed`, `escalate`, or `cancelled`. Use `--allow-lock-conflict` only
when the leader explicitly approves overlapping claims; `validate` treats those
overrides as intentional. `status` shows active write claims so the scheduler
can enforce isolation without reading worker context.

## Validation

```bash
python v2/tests/unit_test.py
python v2/tests/smoke_test.py
python v2/tests/local_backend_contract_test.py
python v2/tests/tmux_contract_test.py
```

PowerShell compile check:

```powershell
$files = @('v2/run.py') + (Get-ChildItem -Path 'v2/costmarshal_v2','v2/tests' -Filter '*.py' | ForEach-Object { $_.FullName })
python -m py_compile @files
```

The smoke test covers the scheduler happy path plus important failure paths:
missing read-only source projects, unknown actor recipients, missing terminal
reports, invalid task state transitions, and task/status drift detected by
`validate`. It also verifies durable actor prompt files and recovery repair for
missing prompts. The local backend contract test starts and stops a detached
Python process through the portable backend. The tmux contract test uses a fake
tmux executable to exercise the tmux adapter without requiring tmux to be
installed locally. The smoke test also verifies relay cursor behavior,
duplicate prevention, write-claim conflict blocking, claim release after
terminal task states, result acceptance records, and leader self-work audit
summaries.

## Design Boundary

v2 deliberately keeps scheduler intelligence small. The scheduler does not
plan, implement, review, or summarize raw actor context. It only records
messages, status, actor metadata, and report paths. The leader remains
responsible for decisions; agents remain responsible for bounded task work.
