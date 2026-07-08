---
name: costmarshal
description: "CostMarshal: stable, cost-aware multi-model agent orchestration for Codex CLI. Use when the user wants a leader model to run long complex work or auto-research by creating project directories, branch trees, structured worker briefs, embedded WakeWait-style wait commands, connectivity checks, project/global agent memory, budget controls, escalation gates, and scorecard-based routing across strong, medium, and low-cost subagents."
---

# CostMarshal

## Core Contract

Keep the leader as the project controller, not the bulk implementer.

The leader must:
- define goals and acceptance criteria
- split work into bounded tasks
- choose agents by risk, difficulty, budget, and observed performance
- control token, money, time, and concurrency budgets
- read structured reports instead of raw worker context by default
- decide whether to accept, retry, or escalate
- perform final integration and acceptance

The leader should not write large implementations unless no worker is suitable, the task is on the critical path, or quality/safety requires direct intervention.

If the leader performs direct implementation-like work, record it as an auditable exception with `record-leader-work`. This keeps the leader from quietly becoming the default worker while still allowing small integration, verification, or emergency fixes when delegation would reduce quality.

The senior-demo replay loop exists to expand weaker agents' safe usable range and stability. The leader should classify the proven path as a task type, then save the fully reproducible details in a single replay memory file. Cheaper agents receive that one memory file as task context instead of reading a growing set of reusable documents.

## Script-First Rule

Use `scripts/costmarshal.py` for durable state instead of relying on prose memory. `scripts/mc.py` remains as a compatibility entrypoint.

Common commands:

```bash
python scripts/costmarshal.py init-root
python scripts/costmarshal.py new-project --name my-run --kind arbor --objective "Improve benchmark score with Arbor-style experiments" --mode auto
python scripts/costmarshal.py adopt-project --path <existing-project-dir> --name adopted-run --kind arbor --objective "Continue this existing run under CostMarshal rules" --mode auto
python scripts/costmarshal.py check-agents --project <project-dir>
python scripts/costmarshal.py draft-plan --project <project-dir> --summary "Lightweight direction check; adapt later tasks after first evidence." --predicted-cost-cny 3 --predicted-wall-time "30m"
python scripts/costmarshal.py approve-plan --project <project-dir> --approved-by user --note "User confirmed the plan"
python scripts/costmarshal.py set-leader-review --project <project-dir> --level auto --reason "Adapt leader verification effort to task risk and agent evidence"
python scripts/costmarshal.py recommend --task-type research-ideate --difficulty A --risk medium
python scripts/costmarshal.py new-task --project <project-dir> --title "bounded task" --purpose "..." --agent deepseek --difficulty A --risk medium --task-type analysis --claim-path reports/analysis.md
python scripts/costmarshal.py new-check-task --project <project-dir> --source-task CM-0001 --check "Evidence supports the claim" --check "No unapproved file writes are proposed" --reviewer deepseek
python scripts/costmarshal.py run-task --project <project-dir> --task CM-0001 --dry-run
python scripts/costmarshal.py run-task --project <project-dir> --task CM-0001
python scripts/costmarshal.py wait-task --project <project-dir> --task CM-0001 --every 30s --timeout 1h
python scripts/costmarshal.py record-result --project <project-dir> --task CM-0001 --status done --agent deepseek --model deepseek-v4-flash --quality-score 4 --accepted-by-leader
python scripts/costmarshal.py record-leader-work --project <project-dir> --task CM-0001 --work-type verification --risk low --scope "Sampled evidence and integrated final decision" --reason "Final acceptance requires leader review"
python scripts/costmarshal.py record-handoff --project <project-dir> --source-task CM-0001 --summary "Compressed handoff for reviewer" --next-step "Review evidence"
python scripts/costmarshal.py new-review-task --project <project-dir> --source-task CM-0001 --reviewer kimi
python scripts/costmarshal.py promote-memory --project <project-dir> --source-task CM-0001 --name reusable-flow --memory-task-type mechanical --summary "What the senior agent proved and exactly how to replay it" --working-dir "." --required-input "config.yaml exists" --allowed-param "top_k" --allowed-command "python run_eval.py --config config.yaml" --expected-output "results.json" --success-marker "command exits 0"
python scripts/costmarshal.py new-task --project <project-dir> --title "replay reusable flow" --purpose "Run the proven flow with new parameters" --agent longcat --difficulty B --risk low --task-type mechanical --replay-memory reusable-flow --depends-on CM-0001
python scripts/costmarshal.py record-memory-feedback --project <project-dir> --task CM-0003 --outcome partial --sufficient no --memory-quality 2 --attribution memory_issue --needs-senior-refresh --issue "Missing required input path"
python scripts/costmarshal.py evolve-project --project <project-dir>
python scripts/costmarshal.py status-project --project <project-dir>
python scripts/costmarshal.py finish-project --project <project-dir>
```

Use `--root <dir>` or `COSTMARSHAL_HOME` to choose the global storage location. Default storage is `$CODEX_HOME/costmarshal` when `CODEX_HOME` is set, otherwise `~/.codex/costmarshal`. Legacy `MMC_HOME` and `MULTI_MODEL_CONDUCTOR_HOME` are still accepted.

Always start a new long-running project with `new-project`, or use `adopt-project` when an existing project is already in progress and should be summarized into CostMarshal state. Adoption imports existing progress into `imported-progress.md` and `reusable-candidates.md`, but it does not bypass the plan gate or convert old artifacts into trusted replay memory. Projects default to `--mode auto`: CostMarshal tries cost-saving orchestration when cheap medium/low agents are configured, and automatically falls back to `same-agent` context-control mode when they are not. After the leader drafts a lightweight direction check with coarse cost/time/token predictions, write it with `draft-plan`, show `plan-approval.md` to the user, and continue only after the user confirms and `approve-plan` is recorded. Do not over-plan the whole project up front; later task planning should adapt to worker evidence, replay memory feedback, failed checks, imported evidence, and budget state. New projects default to a CNY 20 project budget; override with `--max-project-cost-cny <amount>` only when the run intentionally needs more. Use live checks only when provider base URL env vars are configured.

## Agent Tiers

Use these as priors, then update routing from scorecard evidence.

| Tier | Role | Default use |
| --- | --- | --- |
| High | Senior strong Codex agent | First-pass exploration, architecture, complex implementation, final review, rescue, skill/runbook creation |
| Medium | DeepSeek or Kimi | Bounded analysis or implementation with clear inputs, outputs, and tests |
| Low | LongCat | Mechanical reuse of a proven script/runbook, parameter changes, extraction, compression, simple formatting |

Never hard-code API keys in prompts, reports, logs, or skill files. Refer to secrets only by environment variable names such as `DEEPSEEK_API_KEY`, `MOONSHOT_API_KEY`, and `LONGCAT_API_KEY`.

## Orchestration Modes

Use `--mode auto` by default. Auto means the default intent is cost-saving, but if no enabled medium/low agent has its required API key configured, CostMarshal records `effective_mode = same-agent` and uses the strong Codex agent form for context management.

| Mode | Use | Routing behavior |
| --- | --- | --- |
| `auto` | Default | Cost-saving if cheap agents are configured; same-agent fallback otherwise |
| `cost-saving` | Save money | Prefer configured cheaper agents for bounded low-risk work |
| `same-agent` | Manage context with strong agents | Prefer `senior` while still splitting tasks, reports, waits, and memory |
| `balanced` | Mixed quality/cost | Prefer senior for uncertain work, cheap agents for bounded work |

`same-agent` is a first-class mode, not a failure. It is useful when the user has no cheap worker keys configured or wants conductor-style context isolation with strong models only.

## Dispatch Loop

1. Run `costmarshal.py new-project` for every new project or long-running research run. If the work already exists, run `costmarshal.py adopt-project --path <existing-project>` instead; this creates a normal CostMarshal project plus imported progress summaries.
2. Run `costmarshal.py check-agents --project <project>` before dispatching workers.
3. Refresh `master-snapshot.md`: goal, non-goals, constraints, acceptance criteria, current risks.
4. Draft only a lightweight high-level approach, coarse predicted cost/time/token range, initial agent allocation, acceptance criteria, verification direction, known risks, and any imported reuse evidence with `costmarshal.py draft-plan`.
5. Present `plan-approval.md` to the user. Do not create worker tasks until the user confirms the plan.
6. After confirmation, run `costmarshal.py approve-plan --project <project>`. `new-task` is blocked until this approval exists unless the leader uses an explicit manual override.
7. Plan incrementally after approval: classify each next task by difficulty, risk, expected context size, write scope, and verification method based on the latest evidence.
8. Use `costmarshal.py recommend` to check global memory before assigning the worker.
9. Use `costmarshal.py new-task` to create the branch card, task brief, status file, completion report template, and branch-tree node. Use `--depends-on` for prerequisites and `--claim-path` for write locks.
10. Keep each round to at most three active workers: one high-tier senior worker, one medium-tier worker, and one low-tier LongCat worker.
11. For OpenAI-compatible medium/low agents, use `costmarshal.py run-task --dry-run` before the first call, then `run-task` to execute the read-only worker runner. Senior Codex subagents still require direct Codex/subagent dispatch.
12. In `same-agent` mode, still create a `senior` task/subagent for nontrivial work; do not let the leader absorb the task just because the same strong model family is being used.
13. Give each worker only its `brief.md`, explicit file paths, approved summaries, and task-local messages.
14. Use status files (`status.json`, `DONE`, `FAILED`, or `ESCALATE`) as the normal return channel.
15. Use CostMarshal's embedded WakeWait-style commands when waiting on worker status, long commands, training, evaluations, or file readiness. Do not require a separate WakeWait skill install and do not spend leader turns polling.
    - `costmarshal.py sleep --duration 10m`
    - `costmarshal.py wait-task --project <project> --task CM-0001 --every 30s --timeout 1h`
    - `costmarshal.py wait-file --path <path> --every 30s --timeout 1h`
    - `costmarshal.py wait-contains --path <log> --text DONE --every 30s --timeout 1h`
    - `costmarshal.py wait-command --command "<check command>" --every 30s --timeout 1h`
16. Read `completion-report.md` first. Read raw worker transcript only for audit, incident review, unclear reports, or explicit user request.
17. For simple bounded checks, create medium-tier verification shards with `costmarshal.py new-check-task`. Use project `set-leader-review --level auto` by default, or override to `high`, `medium`, or `low` when risk or budget requires it.
18. Accept, request rework, or escalate based on verification evidence, not worker confidence alone.
19. Use `costmarshal.py record-result` after every worker attempt to record the leader's final evaluation. `run-task` records a separate usage event automatically from provider token data, but it does not count as leader acceptance.
20. Record `--input-tokens` and `--output-tokens` whenever provider usage is available. If `--estimated-cost-cny` is omitted, CostMarshal estimates cost from known per-agent CNY-per-1M-token prices.
21. If the leader directly writes or fixes implementation-like work instead of dispatching it, immediately run `costmarshal.py record-leader-work --project <project> --scope <small-scope> --reason <why-delegation-was-unsuitable>`. Use it for integration, verification, emergency fixes, and trivial glue; avoid using it as a substitute for worker tasks.
22. After a high-tier senior worker proves a repeatable workflow, classify it by task type and run `costmarshal.py promote-memory --project <project> --source-task <task> --name <memory-name> --memory-task-type <type> ...` to create one replay memory file under `memory/replay/<task-type>/<memory-name>/memory.md`.
23. Assign weak or cheaper agents to replay only after the replay memory exists. Create those tasks with `--replay-memory <memory-name>`, low risk, bounded allowed writes, explicit parameters, and clear success markers.
24. After every replay attempt, run `costmarshal.py record-memory-feedback` so the memory file accumulates quality evidence.
25. If feedback attribution is `memory_issue` or `--needs-senior-refresh`, mark the memory as needing revision and send it back to a senior agent. If attribution is `agent_capability`, adjust routing for that model instead of blaming the memory.
26. Use `costmarshal.py record-handoff` for compressed task-to-task context and `new-review-task` for bounded cross-agent review.
27. Use `costmarshal.py status-project` during long runs to inspect task states, locks, budget, replay memory health, agent cost, each task's concrete model name, short result summary, leader self-work exceptions, leader review policy, and accumulated WakeWait-style wait time.
28. Use `costmarshal.py finish-project` at project completion so the global memory and project summary include per-agent input tokens, output tokens, total tokens, estimated CNY cost, task summaries, model names, leader self-work exceptions, leader review policy, and wait-time totals. `finish-project` runs project evolution by default.
29. Use `costmarshal.py evolve-project` after adding final leader notes or senior abstractions. The evolution phase writes `reports/evolution-report.md`, updates routing evidence, and promotes compact reusable lessons into `memory/knowledge-index.json` plus one small `memory/knowledge/<task-type>/<lesson>.md` file per lesson.
30. For future projects, read the knowledge index first and attach at most one matching knowledge file unless the leader explicitly approves more context. Prefer replay memory for exact command reproduction; use knowledge lessons for common problem patterns, bug fixes, and reusable judgment.

## Existing Project Adoption

Use `adopt-project` when a project is already running outside CostMarshal. Adoption must follow normal design rules:
- create a new CostMarshal project directory
- record the original path in `adopted-project.json`
- write factual progress into `imported-progress.md`
- classify possible reuse into `reusable-candidates.md`
- add imported nodes to the branch tree
- keep `project.json.plan_approval.status = not_drafted`
- require `draft-plan` and user `approve-plan` before new worker tasks

Imported progress is evidence, not acceptance. Imported scripts, logs, reports, checkpoints, and result files are only candidates. Promote them to replay memory only after the normal reproducibility rule is satisfied or a senior agent refreshes the path.

## Senior Demo To Weak Replay Loop

Use this loop for "strong agent proves once, cheaper agents replay":

Purpose: increase weak-agent usefulness and reliability by turning a hard first pass into a narrow, verified, repeatable memory file. The memory file should give weak agents more usable task coverage, not more freedom to improvise.

1. Dispatch an S/A task to `senior` to explore and run the workflow end to end.
2. Verify the senior output through tests, artifacts, logs, or leader review.
3. Record the senior result with token/cost fields and `--accepted-by-leader`.
4. Promote the proven task into one replay memory file with `promote-memory`.
5. Dispatch B/C mechanical replay tasks to LongCat, DeepSeek, or Kimi with `--replay-memory`.
6. Weak agents must follow the replay memory file, change only explicit parameters, and stop rather than inventing fixes.
7. Require replay workers to report memory quality, missing details, and whether failure was caused by memory quality, agent capability, task mismatch, environment, or unknown.
8. Escalate if replay needs new judgment, broader context, non-whitelisted edits, or unproven commands.

## Replay Memory Reproducibility Rule

Replay memory must be complete enough for a weaker agent to reproduce the proven path without the senior raw transcript. Non-draft memory must include:
- source task `status.json` state is `done`
- non-empty source `completion-report.md`
- task classification via `--memory-task-type`
- exact working directory via `--working-dir`
- required inputs via `--required-input`
- allowed parameter changes via `--allowed-param`
- exact allowed commands via `--allowed-command`
- expected outputs via `--expected-output`
- success markers via `--success-marker`
- stop/escalate failure protocol

`promote-memory` must reject incomplete memory unless `--draft` is explicitly set. Draft memory must not be attached to weak-agent tasks.

Replay memory feedback is part of the contract. A memory file being complete does not mean every weak agent can use it successfully. The leader must distinguish:
- `memory_issue`: the memory is missing, ambiguous, or wrong; route to senior to revise and do not keep dispatching weak agents on it
- `agent_capability`: the memory is adequate, but the selected model could not execute it; update routing or escalate the model tier
- `task_mismatch`: the task is outside this memory's class; create or choose a different replay memory
- `environment_issue`: fix dependencies, files, credentials, quota, or runtime state before retrying

## When To Load References

- Read `references/protocol.md` when creating branch cards, task briefs, reports, scorecards, or routing rules.
- Read `references/longcat-mechanical.md` before assigning LongCat a parameterized script/runbook task.
- Read `references/storage.md` before starting, validating, archiving, or repairing a project directory.

## Escalation Rules

Escalate to the high-tier senior worker when:
- the task touches architecture, security, permissions, data deletion, secrets, or irreversible state
- the write scope spans multiple coupled modules
- tests fail after a worker attempt
- a medium-tier worker makes an architectural recommendation
- LongCat changes anything outside the explicit whitelist
- reports conflict or omit required evidence
- the worker marks low confidence or exceeds budget
- final acceptance depends on subtle correctness

## Merge Discipline

Treat worker output as untrusted until verified. Merge only concise, approved summaries into leader context. Preserve raw logs and transcripts as branch-local evidence, not default leader memory.

For code changes, prefer isolated worktrees or disjoint write scopes. The leader owns final integration, conflict resolution, test selection, and acceptance.

## Auto-Research Compatibility

For Arbor, Feynman, or other long research loops, use `--kind arbor`, `--kind feynman`, or `--kind autoresearch`. Treat each phase as a branch-tree task: intake, ideation, executor implementation, evaluation, novelty/search, merge, report. Early in a project, use strict verification because agent ability is unknown. Relax verification only after global memory shows repeated high-quality accepted results for the same agent and task type.
