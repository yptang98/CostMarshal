<p align="center">
  <img src="assets/cover.png" alt="CostMarshal cover" width="100%">
</p>

# CostMarshal v2

CostMarshal v2 is the official scheduler-first line of CostMarshal. It is a
cost-aware control layer for Codex CLI that keeps the leader model in charge of
planning, routing, verification, and final acceptance while task-scoped agent
actors work from bounded briefs and durable mailboxes.

Version: `v2.0.0`

GitHub: https://github.com/yptang98/CostMarshal

## What Changed From v1

v2 no longer uses the old monolithic `scripts/mc.py` runtime as the product
entrypoint. The official CLI is now:

```bash
python scripts/costmarshal.py ...
```

That script loads the v2 package under `costmarshal_v2`. The old v1 engine
is left in `scripts/mc.py` only as legacy reference material for existing users
who intentionally run it directly.

## Architecture

v2 models each project as durable actors:

- scheduler: relay, mailbox writer, process supervisor, recovery auditor
- leader: persistent project controller
- agents: task-scoped workers that read explicit briefs and report back

Actor execution is backend-driven rather than hard-wired to one terminal tool:

| Backend | Intended hosts | Behavior |
| --- | --- | --- |
| `auto` | Default | Windows uses `local`; macOS/Linux use `tmux` when available and otherwise `local`. |
| `local` | Windows, CI, minimal shells | Starts detached local processes and records pid/log paths. |
| `tmux` | Unix servers with tmux | Starts one actor per tmux window and supports runtime text injection. |

The scheduler talks to the backend interface. It does not plan, implement,
review, or read raw actor context.

## Install By Codex Prompt

The same prompt is available in [`INSTALL_PROMPT.md`](INSTALL_PROMPT.md).

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
- Verify Python 3.10+ is available with: python --version
- Run: python <installed-skill>/scripts/costmarshal.py --help
- Run: python <installed-skill>/scripts/costmarshal.py init --name install-smoke --objective "Validate CostMarshal v2 install" --backend local
- Run: python <installed-skill>/scripts/costmarshal.py validate --project <created-project-dir>
- Report the installed path, old version if updated, new version, backup path if created, and validation result.
```

After install, restart Codex if the skill list is cached.

## Quick Start

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

## Runtime State

```text
<runtime-root>/
  projects/<project-id>/
    project.json
    PROTOCOL.md
    scheduler/
      session.json
      events.jsonl
      relay-cursors.json
      actors/
      mailboxes/
    tasks/
    reports/
      results.jsonl
      leader-work.jsonl
    transcripts/
    locks/
```

Each actor has a durable prompt file in `scheduler/actors/*.prompt.md`. If a
session is interrupted, restart or recover from those prompt files, mailbox
files, and task status files rather than relying on chat memory.

## Leader Discipline

The leader should plan, route, verify, integrate, and accept. It should not
silently become the default worker. If the leader performs direct
implementation-like work, record it immediately:

```bash
python scripts/costmarshal.py record-leader-work --project <project-id> --task V2-0001 --work-type integration --risk low --scope "Small final glue edit" --reason "Delegation would add coordination risk"
```

After every worker attempt, record the leader's final evaluation:

```bash
python scripts/costmarshal.py record-result --project <project-id> --task V2-0001 --status done --quality-score 4 --accepted-by-leader --summary "Accepted after evidence check"
```

## Validation

```bash
python tests/unit_test.py
python tests/smoke_test.py
python tests/local_backend_contract_test.py
python tests/tmux_contract_test.py
python scripts/install_smoke_test.py
```

PowerShell compile check:

```powershell
$files = @('scripts/costmarshal.py') + (Get-ChildItem -Path 'costmarshal_v2','tests','scripts' -Filter '*.py' | ForEach-Object { $_.FullName })
python -m py_compile @files
```

## Design Boundary

CostMarshal v2 is deliberately scheduler-first. The scheduler is not another
thinking agent; it is a small durable control plane. The leader remains
responsible for decisions, while agent actors remain responsible for bounded
task work and structured reports.

