<p align="center">
  <img src="assets/cover.png" alt="CostMarshal cover" width="100%">
</p>

# CostMarshal v2

<p align="center">
  <img alt="Codex Skill" src="https://img.shields.io/badge/Codex-Skill-111827">
  <img alt="Scheduler first" src="https://img.shields.io/badge/Scheduler--first-v2-2563eb">
  <img alt="Multi agent" src="https://img.shields.io/badge/Multi--agent-Actors-0891b2">
  <img alt="Cost aware" src="https://img.shields.io/badge/Cost--aware-Orchestration-059669">
  <img alt="Leader discipline" src="https://img.shields.io/badge/Leader-Self--work--Gate-b91c1c">
  <img alt="MIT License" src="https://img.shields.io/badge/License-MIT-green">
</p>

CostMarshal v2 is the official scheduler-first line of CostMarshal: a durable,
two-provider control plane that routes bounded work to LongCat and reserves
Codex for management gates, difficult work, escalation, and final acceptance.

The scheduler never calls a model. The Codex manager runs on demand instead of
occupying a persistent management session. Task-scoped actors run through
independent `codex exec` processes, so each task has an explicit provider,
profile, model, prompt, report, token record, and escalation history.

Version: `v2.2.0-beta`

GitHub: https://github.com/yptang98/CostMarshal

## Official v2 Line

v2 replaces the old monolithic runtime as the product line. The official entry
point is:

```bash
python scripts/costmarshal.py ...
```

That wrapper loads the `costmarshal_v2` package. The old v1 engine remains in
`scripts/mc.py` only as legacy reference material for existing users who
intentionally call it directly.

## Why CostMarshal

Long Codex sessions are easy to make expensive and hard to audit:

- the strongest model starts reading every log, file, and worker transcript
- cheap models receive too much context and improvise outside their ability
- task ownership and write scopes become blurry
- terminal or network disconnects lose what each agent was supposed to do
- token/cost records and final acceptance decisions are scattered

CostMarshal v2 turns this into a software-engineered workflow:

- **Scheduler-first control plane:** the scheduler relays messages, launches
  actors, records events, and audits recovery; it does not plan or review.
- **Real provider rotation:** low-risk bounded tasks default to the `longcat`
  Codex profile; failures and explicit escalations launch a fresh Codex actor.
- **On-demand manager:** Codex is invoked for planning, review, integration, or
  rescue only when the workflow reaches a management gate.
- **Durable actors:** the leader and every worker have prompt files, mailbox
  files, runtime state, and task bindings on disk.
- **Leader discipline:** direct leader implementation-like work must be
  recorded with reason, scope, time, token estimates, and cost.
- **Task-scoped workers:** agents get a bounded brief, explicit context, and
  allowed write paths.
- **Write-lock safety:** active `--claim-path` overlaps are rejected unless the
  leader explicitly overrides them.
- **Cost visibility:** `record-usage`, `record-result`, and
  `record-leader-work` preserve input tokens, output tokens, total tokens,
  estimated CNY cost, quality, and acceptance.
- **Live dashboard:** `dashboard --watch` shows the scheduler, leader, agent
  processes, mailbox counts, runtime pid/target, log paths, and per-agent
  cumulative token totals.
- **Recovery by files, not memory:** prompts, mailboxes, status files, and
  backend runtime metadata are enough to resume after interruption.

## Install By Codex Prompt

The same prompt is available in [`INSTALL_PROMPT.md`](INSTALL_PROMPT.md).

Open a Codex session and paste:

```text
Install CostMarshal from https://github.com/yptang98/CostMarshal into my Codex skills directory.

Requirements:
- Clone or download https://github.com/yptang98/CostMarshal.
- Resolve the Codex skills directory: $CODEX_HOME/skills if CODEX_HOME is set, otherwise ~/.codex/skills.
- If costmarshal is not installed, copy the skill folder to <skills-dir>/costmarshal.
- If <skills-dir>/costmarshal already exists, treat this as an update:
  - Read <skills-dir>/costmarshal/VERSION if it exists and report the old version.
  - Move the old installed skill folder to <skills-dir>/costmarshal.backup-<timestamp>.
  - Copy the new CostMarshal skill folder to <skills-dir>/costmarshal.
  - Do not copy .git, __pycache__, .env files, local runtime folders, or secret files.
  - Preserve $CODEX_HOME/costmarshal-v2 or ~/.codex/costmarshal-v2 runtime state exactly as-is.
  - Preserve legacy $CODEX_HOME/costmarshal or ~/.codex/costmarshal runtime state exactly as-is.
  - Preserve local secret files exactly as-is; do not print secret values.
- Do not copy any local .env files or secrets.
- Verify Python 3.10+ is available with: python --version
- If python is unavailable on Windows, try: py -3 --version
- CostMarshal v2 uses scheduler actors and pluggable runtime backends; do not run legacy v1 initialization unless I explicitly ask for it.
- Run: python <installed-skill>/scripts/costmarshal.py --help
- Run: python <installed-skill>/scripts/costmarshal.py init --name install-smoke --objective "Validate CostMarshal v2 install" --backend local
- Run: python <installed-skill>/scripts/costmarshal.py run-scheduler --project <created-project-dir> --once
- Run: python <installed-skill>/scripts/costmarshal.py dashboard --project <created-project-dir> --format json
- Run: python <installed-skill>/scripts/costmarshal.py validate --project <created-project-dir>
- Tell me I can invoke it with `$costmarshal`, for example: `$costmarshal start a new Arbor project for ...`
- Run skill validation if quick_validate.py is available.
- Report the installed path, old version if updated, new version, backup path if created, and validation result.
```

After install, restart Codex if the skill list is cached.

## Invoke In Codex

Use the skill directly:

```text
  $costmarshal start a cost-aware project. Route bounded work to LongCat, escalate failures to Codex, and invoke the Codex manager only at planning and acceptance gates.
```

Natural language works too:

```text
Use CostMarshal v2 for this long task. Prefer LongCat for verifiable bounded work, use Codex for difficult or high-risk work, and preserve recovery and acceptance state.
```

## Quick Start

```bash
python scripts/costmarshal.py configure-profiles
# Set LONGCAT_API_KEY in the environment or a local secrets file. Never commit it.

python scripts/costmarshal.py init --name demo --objective "Try cost-aware rotation" --workspace . --backend auto

# Keep this running in a scheduler terminal.
python scripts/costmarshal.py run-scheduler --project <project-id> --interval 2

# Keep this running in a dashboard terminal.
python scripts/costmarshal.py dashboard --project <project-id> --watch

# Auto routing chooses LongCat for this low-risk bounded task.
python scripts/costmarshal.py new-task --project <project-id> --title "Inspect baseline" --purpose "Return a bounded report" --task-type analysis --risk low --provider auto --claim-path reports/baseline.md
python scripts/costmarshal.py dispatch --project <project-id> --task V2-0001 --start

# LongCat failures or `Status: escalate` reports automatically route to Codex.
# A manual override is also available:
python scripts/costmarshal.py escalate --project <project-id> --task V2-0001 --reason "Needs architectural judgment" --start

# Invoke the manager only for a planning/review/integration gate.
python scripts/costmarshal.py run-manager --project <project-id>

python scripts/costmarshal.py collect --project <project-id> --task V2-0001 --state waiting_leader --summary "Worker report is ready for leader review"
python scripts/costmarshal.py record-result --project <project-id> --task V2-0001 --status done --quality-score 4 --accepted-by-leader --summary "Accepted after evidence check"
python scripts/costmarshal.py record-leader-work --project <project-id> --task V2-0001 --work-type verification --risk low --scope "Sampled evidence" --reason "Leader acceptance requires review"
python scripts/costmarshal.py stop-actor --project <project-id> --actor agent-v2-0001 --reason "task complete"
python scripts/costmarshal.py status --project <project-id>
python scripts/costmarshal.py recover --project <project-id> --plan-restarts
python scripts/costmarshal.py validate --project <project-id>
```

Use `--root <dir>` or `COSTMARSHAL_V2_HOME` to choose the runtime root. Default
storage is `$CODEX_HOME/costmarshal-v2` when `CODEX_HOME` is set, otherwise
`~/.codex/costmarshal-v2`.

### Provider profiles

`configure-profiles` creates a user-level `longcat.config.toml` containing the
LongCat Responses endpoint and `env_key = "LONGCAT_API_KEY"`. It never writes
the key itself. The default Codex actor keeps the current user profile and
authentication. You may also select profiles explicitly with
`new-task --profile`, `dispatch --profile`, or `run-manager --profile`.

The actor runner loads an optional `init --secrets-file <path>` only into child
process environments. Secrets are never copied into prompts, actor state,
reports, or repository files.

Automatic routing is intentionally small:

- low/medium-risk analysis, documentation, extraction, mechanical work,
  summarization, tests, verification, and small edits default to LongCat
- high-risk, hard, or unbounded work defaults to Codex
- a failed LongCat process or a report containing `Status: escalate` creates a
  new Codex attempt when automatic escalation is enabled
- every provider attempt keeps its own report under `tasks/<id>/attempts/`;
  `completion-report.md` always points to the latest attempt

## Architecture

v2 models each project as durable actors:

| Actor | Responsibility | Must Not Do |
| --- | --- | --- |
| `scheduler` | Relay mailboxes, execute structured actor commands, start/stop runtimes, write state, enforce locks, audit recovery | Plan, implement, review, summarize raw reasoning |
| `leader` | On-demand Codex manager for goals, task boundaries, review, integration, and acceptance | Poll, stay permanently active, or become the hidden default worker |
| `agent-*` | Execute one bounded task through an explicit Codex or LongCat profile | Broaden context, change write scope, expose secrets, make architecture decisions |

Actor execution is backend-driven:

| Backend | Intended Hosts | Behavior |
| --- | --- | --- |
| `auto` | Default | Windows uses `local`; macOS/Linux use `tmux` when available, otherwise `local` |
| `local` | Windows, CI, minimal shells | Starts detached local processes and records pid/log paths |
| `tmux` | Unix servers with tmux | Starts one actor per tmux window and supports runtime text injection |

## Runtime State

```text
<runtime-root>/
  projects/<project-id>/
    project.json
    PROTOCOL.md
    scheduler/
      state.json
      session.json
      events.jsonl
      relay-cursors.json
      actors/
        leader.json
        leader.prompt.md
        agent-v2-0001.json
        agent-v2-0001.prompt.md
      mailboxes/
        leader/
          inbox.jsonl
          outbox.jsonl
        agent-v2-0001/
          inbox.jsonl
          outbox.jsonl
    tasks/
      V2-0001/
        task.json
        brief.md
        status.json
        completion-report.md
    reports/
      results.jsonl
      leader-work.jsonl
      usage.jsonl
    transcripts/
    locks/
      claims.json
```

If a session is interrupted, restart or recover from these files rather than
from chat memory.

## Command Reference

| Command | Purpose |
| --- | --- |
| `configure-profiles` | Create `$CODEX_HOME/longcat.config.toml` without storing an API key |
| `init` | Create a v2 project, scheduler state, leader actor, protocol file, and backend config |
| `run-manager` | Run one on-demand Codex manager turn |
| `start-leader` | Backward-compatible alias for `run-manager` |
| `new-task` | Create a bounded task with brief, status, report template, context, and write claims |
| `dispatch` | Route and start a task through `codex exec --profile ...` |
| `escalate` | Replace a failed or uncertain LongCat attempt with a Codex attempt |
| `send` | Write a durable mailbox message, optionally injecting it into the runtime |
| `relay` | Relay actor-authored outbox messages using durable cursors |
| `run-scheduler` | Run the waiting loop that relays actor outboxes and executes structured scheduler commands |
| `heartbeat` | Record actor liveness and advance running task state |
| `collect` | Mark a worker report ready for leader review |
| `record-usage` | Record actor-reported input/output token usage while work is in progress |
| `record-result` | Record leader acceptance/rejection, quality, token usage, and cost |
| `record-leader-work` | Audit direct leader implementation-like work |
| `stop-actor` | Mark an actor stopped and optionally stop its runtime process |
| `recover` | Audit missing prompts, mailboxes, runtimes, and restart plans |
| `status` | Show actors, mailboxes, relay cursors, results, leader work, locks, and tasks |
| `dashboard` | Show a process board for scheduler, leader, agents, liveness, mailboxes, logs, and token totals |
| `validate` | Validate v2 project structure and ledger consistency |

## Leader Discipline

The manager should plan, route, verify, integrate, and accept in bounded
on-demand turns. It should not silently become the default worker. If it performs direct
implementation-like work, record it immediately:

```bash
python scripts/costmarshal.py record-leader-work --project <project-id> --task V2-0001 --work-type integration --risk low --scope "Small final glue edit" --reason "Delegation would add coordination risk"
```

After every worker attempt, record the leader's final evaluation:

```bash
python scripts/costmarshal.py record-result --project <project-id> --task V2-0001 --status done --quality-score 4 --accepted-by-leader --summary "Accepted after evidence check"
```

`status` surfaces both ledgers:

- worker result records: accept rate, quality, input/output tokens, estimated cost
- leader self-work records: reason, risk, minutes, input/output tokens, estimated cost

## Existing Projects

Use `init --source-project <path>` to bring an already-running project under v2
control without mutating that source project:

```bash
python scripts/costmarshal.py init --name adopted-run --objective "Continue this existing run under v2 control" --source-project <existing-project> --backend auto
```

The source project is treated as read-only reference material. v2 writes its
own project state under the CostMarshal v2 runtime root.

## Scheduler Loop And Dashboard

`run-scheduler` is the small waiting loop. It repeatedly relays actor outboxes,
processes messages addressed to `scheduler`, executes only validated structured
commands, writes scheduler heartbeat state, then waits for the next cycle.

The default runner records reports, usage, collection, and escalation after the
model exits, so Codex and LongCat do not spend tokens editing control-plane
files. Custom or legacy actors can still ask for scheduler actions by appending
JSONL messages to their outbox:

```json
{"from":"leader","to":"scheduler","subject":"scheduler.command","metadata":{"command":"dispatch_task","args":{"task":"V2-0001","provider":"auto","start":true}}}
```

Supported scheduler commands are `create_task`, `dispatch_task`,
`escalate_task`, `collect_task`, `record_result`, `record_usage`, `heartbeat`, and
`stop_actor`. Leader-only commands stay leader-only; agents may report their
own usage, heartbeat, collection, and stop requests.

`dashboard --watch` is the visual process board. It shows the scheduler row,
leader row, every agent row, backend pid/target, alive status, log path,
mailbox counts, task bindings, recent events, and per-agent cumulative token
totals.

## Design Boundary

CostMarshal v2 is deliberately scheduler-first. The scheduler is not another
thinking agent; it is a small durable control plane. Codex remains responsible
for management decisions at explicit gates. LongCat receives bounded,
verifiable work first, and deterministic escalation replaces it with Codex
when the cheap attempt fails or requests broader judgment.

This boundary is what makes the system useful for long tasks: the leader can
stay clean and decisive, workers can be restarted or replaced, and the project
can be audited from disk.

## Validation

Run these checks before publishing or after changing the runtime:

```bash
python tests/unit_test.py
python tests/smoke_test.py
python tests/local_backend_contract_test.py
python tests/tmux_contract_test.py
python tests/model_rotation_contract_test.py
python scripts/install_smoke_test.py
```

PowerShell compile check:

```powershell
$files = @('scripts/costmarshal.py') + (Get-ChildItem -Path 'costmarshal_v2','tests','scripts' -Filter '*.py' | ForEach-Object { $_.FullName })
python -m py_compile @files
```

## Repository Metadata

Suggested GitHub description:

```text
Scheduler-first, cost-aware multi-agent orchestration for Codex CLI with durable actors, mailboxes, write locks, recovery, and leader acceptance ledgers.
```

Suggested GitHub topics:

```text
codex, codex-cli, multi-agent, ai-agents, agent-orchestration, cost-aware-ai, scheduler, tmux, llmops, research-automation
```

## License

MIT License. See [LICENSE](LICENSE).

## Acknowledgements

CostMarshal is inspired in part by [Jinghao67/conductor](https://github.com/Jinghao67/conductor)
and the model-hierarchy-skill approach to tiered model collaboration.
