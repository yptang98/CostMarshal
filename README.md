<p align="center">
  <img src="assets/cover.png" alt="CostMarshal cover" width="100%">
</p>

# CostMarshal

<p align="center">
  <img alt="Codex skill" src="https://img.shields.io/badge/Codex-Skill-111827">
  <img alt="Multi-model orchestration" src="https://img.shields.io/badge/Multi--model-Orchestration-2563eb">
  <img alt="Cost aware" src="https://img.shields.io/badge/Cost--aware-Routing-059669">
  <img alt="Strong-demo replay" src="https://img.shields.io/badge/Strong--demo-Replay-f59e0b">
  <img alt="Project evolution" src="https://img.shields.io/badge/Project-Self--evolution-7c3aed">
  <img alt="WakeWait embedded" src="https://img.shields.io/badge/WakeWait-Embedded-0f766e">
  <img alt="MIT License" src="https://img.shields.io/badge/License-MIT-green">
</p>

CostMarshal is a cost-aware multi-model control layer for Codex CLI.

It lets the expensive leader model stay in charge of planning, routing, verification, and final integration, while cheaper worker models handle bounded tasks from structured briefs. The point is not just to call more models; it is to make long-running work cheaper without losing state, quality control, or the leader's clean context.

CostMarshal is built around five advantages:
- **Cost control:** project budgets, per-task caps, provider token usage, and per-agent CNY cost summaries.
- **Quality control:** user-approved plans, branch-tree tasks, dependencies, write locks, completion reports, review tasks, and escalation gates.
- **Strong-demo replay:** a strong agent can solve an uncertain path once, the leader promotes it into replay memory, and cheaper agents reuse that exact procedure for repeated or similar work.
- **Visible orchestration:** project status shows each task's agent, concrete model name, short result summary, replay memory, and accumulated WakeWait-style wait time.
- **Learning over time:** global agent memory records which model performs well for each task type, so routing improves across projects.

This strong-demo replay loop is the main cost-saving pattern: spend expensive tokens once to discover and verify the path, then use cheaper agents to repeat the same class of task from a complete memory file instead of asking them to improvise.

## Example Workflow

```text
1. Leader drafts a lightweight plan and asks the user to approve cost, time, risks, and acceptance checks.
2. Senior agent solves the uncertain path once and returns a structured completion report.
3. Leader verifies the result, records token/cost usage, and promotes the proven steps into replay memory.
4. Cheaper agents receive only the bounded task brief plus that replay memory file.
5. CostMarshal waits without polling, records each worker's model name, summary, tokens, cost, and wait time.
6. At project finish, CostMarshal evolves global routing memory and writes compact knowledge lessons for future projects.
```

The result is a practical loop: strong model for discovery, cheaper models for repeatable execution, and project memory that gets sharper after every run.

GitHub: https://github.com/yptang98/CostMarshal

Version: `v0.0.2`

## Install By Codex Prompt

The same prompt is also available in [`INSTALL_PROMPT.md`](INSTALL_PROMPT.md).

Open a Codex session and paste:

```text
Install CostMarshal from https://github.com/yptang98/CostMarshal into my Codex skills directory.

Requirements:
- Clone or download https://github.com/yptang98/CostMarshal.
- Copy the skill folder to $CODEX_HOME/skills/costmarshal, or to ~/.codex/skills/costmarshal if CODEX_HOME is unset.
- Do not copy any local .env files or secrets.
- Verify Python 3.10+ is available with: python --version
- Do not install WakeWait separately; CostMarshal bundles embedded WakeWait-style wait commands.
- Run: python <installed-skill>/scripts/costmarshal.py init-root
- Run: python <installed-skill>/scripts/costmarshal.py --help
- Report the installed path and validation result.
```

After install, restart Codex if the skill list is cached.

## Invoke In Codex

Use the skill directly with `$costmarshal`:

```text
$costmarshal start a new Feynman-style research project for optimizing my benchmark. Initialize project storage, check configured agents, draft the initial plan and prediction for my confirmation, and wait for approval before creating worker tasks.
```

You can also ask naturally:

```text
Use CostMarshal to start a new Arbor project for this objective, check agent connectivity, draft the initial plan and cost/time prediction for my confirmation, then split tasks only after I approve.
```

## Quick Start

```bash
python scripts/costmarshal.py init-root
python scripts/costmarshal.py new-project --kind arbor --name demo --objective "Try a bounded research workflow"
python scripts/costmarshal.py check-agents --project <project-dir>
python scripts/costmarshal.py draft-plan --project <project-dir> --summary "Start with a lightweight baseline inspection, then adapt task routing from the first evidence." --predicted-cost-cny 3 --predicted-wall-time "30m"
python scripts/costmarshal.py approve-plan --project <project-dir> --approved-by user --note "User confirmed initial plan"
python scripts/costmarshal.py recommend --task-type research-ideate --difficulty A --risk medium
python scripts/costmarshal.py new-task --project <project-dir> --title "First hypothesis" --purpose "Create a bounded hypothesis" --agent deepseek --difficulty A --risk medium --task-type research-ideate --claim-path reports/idea.md
python scripts/costmarshal.py run-task --project <project-dir> --task CM-0001 --dry-run
python scripts/costmarshal.py run-task --project <project-dir> --task CM-0001
python scripts/costmarshal.py wait-task --project <project-dir> --task CM-0001 --every 30s --timeout 1h
python scripts/costmarshal.py record-result --project <project-dir> --task CM-0001 --agent deepseek --model deepseek-v4-flash --status done --quality-score 4 --accepted-by-leader
python scripts/costmarshal.py record-handoff --project <project-dir> --source-task CM-0001 --summary "Compressed handoff for reviewer" --next-step "Review evidence"
python scripts/costmarshal.py new-review-task --project <project-dir> --source-task CM-0001 --reviewer kimi
python scripts/costmarshal.py promote-memory --project <project-dir> --source-task CM-0001 --name reusable-flow --memory-task-type mechanical --summary "Exact replayable procedure" --working-dir "." --required-input "config.yaml exists" --allowed-param "top_k" --allowed-command "python run_eval.py --config config.yaml" --expected-output "results.json" --success-marker "command exits 0"
python scripts/costmarshal.py new-task --project <project-dir> --title "Replay reusable flow" --purpose "Run the proven flow with new parameters" --agent longcat --difficulty B --risk low --task-type mechanical --replay-memory reusable-flow --depends-on CM-0001
python scripts/costmarshal.py record-memory-feedback --project <project-dir> --task CM-0003 --outcome succeeded --sufficient yes --memory-quality 5 --attribution unknown
python scripts/costmarshal.py evolve-project --project <project-dir>
python scripts/costmarshal.py status-project --project <project-dir>
python scripts/costmarshal.py finish-project --project <project-dir>
```

## Why CostMarshal

Running multiple models directly is easy to make messy: keys leak into prompts, weak models improvise beyond their ability, expensive models get dragged into routine work, and long projects lose track of which agent did what. CostMarshal turns that into a software-engineered workflow.

The leader keeps the mission control view:
- objective, acceptance criteria, and approved plan
- branch tree and task dependencies
- budgets, token usage, and agent costs
- structured reports and final verification

Workers get narrow execution lanes:
- one task brief
- explicit allowed context
- optional replay memory
- clear status signals
- no default access to other workers' raw context

That separation is what makes cheap models useful without letting them quietly take over decisions they should not make.

## Core Workflow

1. **Plan and confirm lightly:** create a project, check agent connectivity, draft the coarse direction and cost/time/token prediction, then wait for user approval.
2. **Dispatch bounded tasks incrementally:** create branch-card tasks with dependencies, write claims, context limits, and budgets as evidence arrives.
3. **Run cheap workers safely:** use the read-only OpenAI-compatible runner for DeepSeek, Kimi, LongCat, or other configured providers.
4. **Review before trusting:** the leader reads `completion-report.md`, verifies evidence, and records final quality with `record-result`.
5. **Turn hard paths into cheap repeats:** if the task pattern will recur, let a strong agent prove it once, promote the result into replay memory, and dispatch cheaper agents against that memory for repeated or same-type tasks.
6. **Evolve after projects:** `finish-project` updates routing evidence, writes an evolution report, and promotes compact reusable lessons into a hierarchical knowledge index.

## Environment Requirements

CostMarshal v0.0.2 depends on Python for its deterministic CLI:

| Dependency | Required | Notes |
| --- | --- | --- |
| Codex skills support | Yes | Required for `$costmarshal` invocation |
| Python 3.10+ | Yes | The CLI uses only the Python standard library |
| Third-party Python packages | No | No `pip install` step is needed |
| Network access | Optional | Needed only for `check-agents --live` provider calls |
| GitHub CLI (`gh`) | No | Useful only for publishing this repository, not for normal use |

## Validation

Run these checks before publishing or after changing the CLI:

```bash
python scripts/smoke_test.py
python scripts/install_smoke_test.py
python -m py_compile scripts/costmarshal.py scripts/mc.py scripts/smoke_test.py scripts/install_smoke_test.py
```

`smoke_test.py` creates a temporary project and verifies the core workflow: default CNY 20 budget, user plan gate, task creation, result recording, wait telemetry, replay memory promotion, weak-agent replay attachment, draft-memory blocking, status output, final project summary, project evolution, and hierarchical knowledge indexing.

`install_smoke_test.py` simulates installing into a temporary `CODEX_HOME`, runs the installed CLI, verifies default runtime storage, then uninstalls the skill directory while preserving runtime state.

Check Python:

```bash
python --version
```

If `python` is unavailable on Windows, install Python or use the Python launcher if present:

```powershell
py -3 --version
py -3 scripts/costmarshal.py --help
```

## Capability Map

| Area | What CostMarshal Provides | Why It Matters |
| --- | --- | --- |
| Project control | durable project directories, approved plan files, branch trees, task cards | long jobs stay recoverable and auditable |
| Delegation safety | bounded briefs, allowed context, dependencies, write claims, status files | workers do not see or touch more than they should |
| Cost accounting | default CNY 20 project budget, task caps, provider usage events, per-agent summaries | cheap work stays cheap, expensive work is visible |
| Worker execution | read-only OpenAI-compatible `run-task` for configured providers | DeepSeek/Kimi/LongCat can produce reports without polluting leader context |
| Quality gates | completion reports, review tasks, leader `record-result`, strict early verification | model output is evidence to verify, not truth to merge blindly |
| Strong-demo replay | senior pathfinding, replay memory promotion, reproducibility contract, weak-agent replay feedback | expensive discovery becomes a reusable low-cost procedure |
| Live task ledger | `status-project` task table with agent, model, summary, replay memory, and wait time | users can see who did what and how long orchestration waited |
| Self-evolution | project evolution report, routing policy update, hierarchical knowledge index | every completed project can make future routing and reuse sharper |
| Waiting | embedded WakeWait-style sleep/file/text/command/task waits | long runs do not waste leader tokens polling |
| Research loops | Arbor/Feynman/autoresearch project kinds and task types | research workflows can be decomposed into measurable phases |

## Agent Model

CostMarshal treats model tiers as priors, then updates them from evidence:

| Tier | Role | Default use |
| --- | --- | --- |
| High | Senior Codex agent | Architecture, complex implementation, rescue, final review |
| Medium | DeepSeek / Kimi | Bounded analysis, local implementation, review, verification |
| Low | LongCat | Mechanical execution of proven scripts/runbooks, extraction, compression |

## Budget Defaults

New projects default to `--max-project-cost-cny 20`. CostMarshal rejects planned tasks when their `--max-cost-cny` would exceed the remaining project budget. Override with `new-project --max-project-cost-cny <amount>` when a run is intentionally larger.

## User Plan Approval

After `new-project` and `check-agents`, the leader must draft the initial direction and coarse predictions:

```bash
python scripts/costmarshal.py draft-plan --project <project-dir> --summary "Lightweight direction check; adapt after first worker evidence." --predicted-cost-cny 3 --predicted-wall-time "30m"
```

The first plan should be a lightweight direction check, not a full project blueprint. `draft-plan` only requires `--summary`; add cost/time/token predictions and a few coarse steps when useful, then let later task planning adapt to worker evidence, replay memory feedback, failed checks, and budget state.

Show `plan-approval.md` to the user. Continue only after the user confirms:

```bash
python scripts/costmarshal.py approve-plan --project <project-dir> --approved-by user
```

`new-task` is blocked until the plan is approved. Use `--allow-unapproved-plan` only when the user explicitly approved outside the stored CostMarshal state.

## Layout

```text
costmarshal/
  SKILL.md
  README.md
  VERSION
  agents/openai.yaml
  assets/cover.png
  references/
  scripts/
    costmarshal.py
    mc.py
    smoke_test.py
    install_smoke_test.py
    wakewait.ps1
    wakewait.sh
```

Runtime state is stored outside the skill:

```text
<CostMarshal root>/
  config/agents.json
  memory/agent-memory.json
  memory/events.jsonl
  memory/evolution-events.jsonl
  memory/evolution-policy.json
  memory/knowledge-index.json
  memory/knowledge/<task-type>/<lesson>.md
  projects/<timestamp>-<slug>/
```

The default root is `$CODEX_HOME/costmarshal` or `~/.codex/costmarshal`. Override with `--root` or `COSTMARSHAL_HOME`.

Project-local replay memory files live inside each project:

```text
<project>/
  memory/
    replay/
      <task-type>/
        <memory-name>/
          memory.md
          metadata.json
```

## Project Evolution

At project completion, CostMarshal should evolve from the evidence it just collected:

```bash
python scripts/costmarshal.py finish-project --project <project-dir>
```

`finish-project` writes `reports/project-summary.md`, then runs project evolution by default. The evolution phase writes `reports/evolution-report.md`, appends evidence to `memory/evolution-events.jsonl`, updates `memory/evolution-policy.json`, and promotes compact reusable lessons into the global hierarchical knowledge index.

You can rerun evolution after adding leader notes:

```bash
python scripts/costmarshal.py evolve-project --project <project-dir> --max-lessons 8 --min-quality 4
```

The knowledge system is deliberately hierarchical to control retrieval cost. Future projects should read the small `memory/knowledge-index.json` first, match by task type and lesson kind, and attach at most one relevant `memory/knowledge/<task-type>/<lesson>.md` file unless the leader approves more context. Use replay memory for exact command-level reproduction; use knowledge lessons for common problem patterns, bug fixes, and reusable judgment.

## Embedded Waits

CostMarshal bundles WakeWait-style waiting, so WakeWait does not need to be installed as a separate skill.

```bash
python scripts/costmarshal.py sleep --duration 10m
python scripts/costmarshal.py wait-task --project <project-dir> --task CM-0001 --every 30s --timeout 1h
python scripts/costmarshal.py wait-file --path <path> --every 30s --timeout 1h
python scripts/costmarshal.py wait-contains --path <log> --text DONE --every 30s --timeout 1h
python scripts/costmarshal.py wait-command --command "<check command>" --every 30s --timeout 1h
```

`wait-task` records durable wait telemetry in `memory/wait-events.jsonl`. `status-project` and `finish-project` surface the accumulated wait time next to each task, alongside the assigned agent, concrete model name, and short completion summary.

The bundled compatibility scripts are also available at `scripts/wakewait.ps1` and `scripts/wakewait.sh`.

## Read-Only Worker Runner

`run-task` executes one planned task with an OpenAI-compatible configured agent. It reads the task `brief.md` and allowed context, runs a preflight budget estimate, calls the provider API, writes raw output to `raw/worker-output.md`, writes the worker report to `completion-report.md`, updates `status.json`, and records provider usage in project/global memory.

Always inspect the call first:

```bash
python scripts/costmarshal.py run-task --project <project-dir> --task CM-0001 --dry-run
```

Then run:

```bash
python scripts/costmarshal.py run-task --project <project-dir> --task CM-0001
```

The v1 runner is intentionally read-only. It does not execute shell commands and cannot directly modify repository files. Use it for analysis, summarization, review, patch plans, and replay-memory-guided mechanical reasoning. Senior Codex subagents are still dispatched by Codex/subagent tooling rather than this OpenAI-compatible runner.

`run-task` records token/cost usage and the concrete provider model separately from leader evaluation. After inspecting `completion-report.md`, run `record-result` without repeating token counts to record acceptance, quality, tests, and final outcome. For manually dispatched senior/subagent work, pass `--model <model-name>` when known so later status and summaries show the model that actually ran.

## Strong Demo To Cheap Replay

CostMarshal supports a deliberate "strong agent proves it once, cheaper agents replay it" loop. This is the part that turns one expensive run into many cheaper repeats.

The purpose is to make weaker agents useful on more tasks while keeping them stable. A strong agent spends the expensive tokens once to discover the safe path; the leader classifies the task type and compresses the path into one complete replay memory file with exact inputs, parameters, commands, expected outputs, success markers, and escalation rules. Later, weak agents receive only that memory file and the new parameters, so they can reproduce the workflow without reading the senior raw transcript.

1. Assign the first uncertain or complex pass to `senior`.
2. Verify the result and record token/cost usage.
3. Promote the verified task into a complete replay memory file:

```bash
python scripts/costmarshal.py promote-memory --project <project-dir> --source-task CM-0001 --name reusable-flow --memory-task-type mechanical --summary "Exact replayable procedure" --working-dir "." --required-input "config.yaml exists" --allowed-param "top_k" --allowed-command "python run_eval.py --config config.yaml" --expected-output "results.json" --success-marker "command exits 0"
```

4. Give cheaper agents bounded replay tasks that include only that memory file:

```bash
python scripts/costmarshal.py new-task --project <project-dir> --title "Replay on shard 2" --purpose "Run the proven flow with shard=2" --agent longcat --difficulty B --risk low --task-type mechanical --replay-memory reusable-flow --depends-on CM-0001
```

Non-draft replay memory must be fully reproducible. `promote-memory` rejects incomplete memory unless `--draft` is set; draft memory is not eligible for weak-agent replay. Weak agents must follow the attached memory file, change only explicit parameters, and stop or escalate instead of inventing a new method.

Use this loop for repeated evaluations, benchmark runs, report templates, log compression, data-shard processing, or any task where the first pass requires judgment but later passes are mostly procedural.

After a replay task, record whether the memory file was actually usable:

```bash
python scripts/costmarshal.py record-memory-feedback --project <project-dir> --task CM-0002 --outcome partial --sufficient no --memory-quality 2 --attribution memory_issue --needs-senior-refresh --issue "Missing required input path"
```

Memory feedback uses attribution to guide the leader: `memory_issue` sends the memory back to a strong agent for revision, while `agent_capability` means the memory may be fine but that model should be routed away from this task type.

## Secrets

Do not commit API keys. Keep them in a local secrets file or environment variables.

Recommended local file:

```text
D:\codex\.sandbox-secrets\costmarshal.env
```

CostMarshal automatically loads local secret files during `check-agents`. It checks common locations such as:

```text
$CODEX_HOME/.sandbox-secrets/costmarshal.env
~/.codex/.sandbox-secrets/costmarshal.env
<CostMarshal root>/config/secrets.env
<CostMarshal root>/secrets.env
```

You can also pass an explicit file:

```bash
python scripts/costmarshal.py check-agents --secrets-file D:\codex\.sandbox-secrets\costmarshal.env
```

Example variable names:

```text
DEEPSEEK_API_KEY
DEEPSEEK_BASE_URL
DEEPSEEK_MODEL
DEEPSEEK_TEMPERATURE

MOONSHOT_API_KEY
MOONSHOT_BASE_URL
KIMI_MODEL
KIMI_TEMPERATURE

LONGCAT_API_KEY
LONGCAT_BASE_URL
LONGCAT_MODEL
LONGCAT_TEMPERATURE
```

Current working endpoint patterns:

| Agent | Base URL | Model | Note |
| --- | --- | --- | --- |
| DeepSeek | `https://api.deepseek.com` | `deepseek-v4-flash` | OpenAI-compatible |
| Kimi | `https://api.moonshot.cn/v1` | `kimi-k2.7-code` | Requires `temperature=1` |
| LongCat | `https://api.longcat.chat/openai` | `LongCat-2.0` | OpenAI-compatible |

## Uninstall By Codex Prompt

The same prompt is also available in [`UNINSTALL_PROMPT.md`](UNINSTALL_PROMPT.md).

Open a Codex session and paste:

```text
Uninstall CostMarshal from my Codex skills directory.

Requirements:
- Remove $CODEX_HOME/skills/costmarshal, or ~/.codex/skills/costmarshal if CODEX_HOME is unset.
- Do not delete CostMarshal runtime state unless I explicitly confirm.
- If I confirm runtime cleanup, remove $CODEX_HOME/costmarshal or ~/.codex/costmarshal.
- Do not delete local secret files unless I explicitly confirm.
- Report exactly what was removed and what was preserved.
```

## Development Checks

```bash
python -m py_compile scripts/mc.py scripts/costmarshal.py
python scripts/costmarshal.py --help
python scripts/costmarshal.py --root <temp-root> init-root
python scripts/costmarshal.py --root <temp-root> validate
```

For live provider checks, keep local keys in one of the CostMarshal secret files, then run:

```bash
python scripts/costmarshal.py --root <temp-root> check-agents --agents deepseek,kimi,longcat --live
```

## v0.0.2 Limitations

- CostMarshal can launch read-only OpenAI-compatible workers with `run-task`, but it does not yet run a full parallel wave scheduler.
- Senior Codex subagents are still invoked through Codex/subagent tooling.
- Token and cost estimates are recorded from caller-provided or provider-reported usage, not enforced by a central billing gateway.
- Verification relaxation is conservative and based on accumulated scorecard evidence.

## License

MIT License. See [LICENSE](LICENSE).

## Acknowledgements

CostMarshal is inspired in part by [Jinghao67/conductor](https://github.com/Jinghao67/conductor) and the model-hierarchy-skill approach to tiered model collaboration.
