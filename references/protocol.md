# CostMarshal Protocol

## Table Of Contents

- Principles
- User plan approval gate
- Task classification
- Routing matrix
- Senior demonstration to replay memory
- Branch card schema
- Worker brief schema
- Dependencies, locks, handoff, and review
- Status schema
- Completion report schema
- Scorecard schema
- Budget policy
- Acceptance flow
- Script commands

## Principles

Use the cheapest agent that can complete the task with verifiable quality. Do not use cheap agents for unbounded judgment. Do not use the leader for bulk implementation unless delegation would increase risk or latency.

Separate context from control:
- leader keeps goals, constraints, decisions, branch map, budgets, and accepted summaries
- workers keep task-local exploration, raw logs, failed attempts, and detailed transcripts
- reports are the default boundary between workers and leader

Use prior tier labels only as initial guesses. Prefer scorecard evidence after enough tasks.

Use `scripts/costmarshal.py` for persistent state. The markdown schemas below define the contract; the script creates and updates the canonical files. `scripts/mc.py` is a compatibility entrypoint.

## User Plan Approval Gate

Before worker tasks are created, the leader must draft a user-visible plan and prediction. The plan must include:
- objective and scope
- proposed steps
- predicted worker tasks
- agent allocation
- predicted cost, wall time, and token use when estimable
- acceptance criteria
- verification plan
- risks and open questions

Use:

```bash
python scripts/costmarshal.py draft-plan --project <project-dir> --summary "..." --step "..." --task "..." --agent-plan "..." --predicted-cost-cny 3 --predicted-wall-time "30m" --acceptance "..." --verification "..." --risk "..."
```

Show `plan-approval.md` to the user. Continue only after the user confirms:

```bash
python scripts/costmarshal.py approve-plan --project <project-dir> --approved-by user
```

`new-task` rejects worker task creation until approval is recorded. Use `--allow-unapproved-plan` only when explicit user confirmation happened outside CostMarshal state and the leader is intentionally recording that exception.

For an existing project, first import progress without changing the source project:

```bash
python scripts/costmarshal.py adopt-project --path <existing-project> --name adopted-run --objective "Continue this run under CostMarshal rules"
```

The imported files are facts and candidates only. The leader must still draft a user-visible plan from the imported evidence and obtain approval before dispatching new worker tasks.

## Task Classification

Classify every task before dispatch.

| Field | Values | Notes |
| --- | --- | --- |
| `difficulty` | `S`, `A`, `B`, `C` | S is hardest and highest-risk |
| `risk` | `high`, `medium`, `low` | Consider correctness, security, data loss, and blast radius |
| `task_type` | `architecture`, `implementation`, `analysis`, `review`, `mechanical`, `summarization`, `verification` | Use the closest primary type |
| `write_scope` | `none`, `single_file`, `disjoint_files`, `coupled_files`, `unknown` | Coupled or unknown scopes need stronger review |
| `verification` | `tests`, `diff_review`, `artifact_exists`, `log_marker`, `human_review`, `none` | Avoid tasks with no verification path |
| `context_size` | `small`, `medium`, `large`, `huge` | Large context can be assigned for extraction, not judgment |

Difficulty defaults:
- `S`: architecture, security, irreversible actions, coupled multi-file implementation, subtle debugging
- `A`: bounded implementation or analysis with clear tests and known modules
- `B`: parameterized runbook/script execution, simple local edits, known patterns
- `C`: extraction, compression, formatting, index generation, simple status reports

## Routing Matrix

| Classification | Preferred agent | Gate |
| --- | --- | --- |
| S or high risk | High-tier senior | Leader review plus tests or explicit acceptance |
| A medium risk | DeepSeek or Kimi, senior if critical | Senior spot-check if code or design matters |
| B mechanical | LongCat if runbook is proven; medium otherwise | Hard whitelist and status evidence |
| C summarization/extraction | LongCat | Leader samples evidence if used for decisions |

Medium-tier choice:
- Prefer Kimi for bounded coding tasks, patch plans, module-local implementation, and code review.
- Prefer DeepSeek for cheaper broad analysis, log triage, comparison, and bounded reasoning with concise output.
- Override with scorecard evidence when a model has proven better or worse on the specific task type.

LongCat choice:
- Prefer LongCat only when the task is mechanical, template-like, or extractive.
- LongCat can run a task the senior worker already demonstrated and converted into a script, command, or runbook.
- LongCat should not make architecture decisions, broad fixes, or unbounded debugging attempts.

## Senior Demonstration To Replay Memory

Use this when the first pass is too hard or uncertain for cheap agents, but later repetitions should be cheap.

The goal is to expand weak-agent usable range and stability. The senior agent performs the judgment-heavy discovery once; the leader classifies the path by task type and compresses it into one replay memory file. Replay gives weak agents more task coverage, not permission to improvise.

1. Create a high-tier `senior` task for the first end-to-end pass.
2. Verify the result through tests, artifacts, logs, or leader review.
3. Record the result with `record-result`, including the concrete model name when known, input/output tokens, and acceptance.
4. Promote the verified task:

```bash
python scripts/costmarshal.py promote-memory --project <project-dir> --source-task CM-0001 --name reusable-flow --memory-task-type mechanical --summary "Exact replayable procedure" --working-dir "." --required-input "config.yaml exists" --allowed-param "top_k" --allowed-command "python run_eval.py --config config.yaml" --expected-output "results.json" --success-marker "command exits 0"
```

5. Assign weaker replay tasks with the replay memory attached:

```bash
python scripts/costmarshal.py new-task --project <project-dir> --title "Replay proven flow" --purpose "Run the proven flow with new parameters" --agent longcat --difficulty B --risk low --task-type mechanical --replay-memory reusable-flow --depends-on CM-0001
```

Replay memory files are stored under `memory/replay/<task-type>/<memory-name>/memory.md`. Weak agents may read that memory file; they must not read the senior raw transcript unless explicitly approved.

Non-draft replay memory must include all reproducibility fields:
- source task status is `done`
- non-empty completion report
- task classification
- working directory
- required inputs
- allowed parameter changes
- exact allowed commands
- expected outputs
- success markers
- failure protocol

`promote-memory` rejects incomplete memory unless `--draft` is set. Draft memory cannot be attached to weak-agent tasks.

After every replay attempt, record memory quality feedback:

```bash
python scripts/costmarshal.py record-memory-feedback --project <project-dir> --task CM-0002 --outcome partial --sufficient no --memory-quality 2 --attribution memory_issue --needs-senior-refresh --issue "Missing required input path"
```

Feedback attribution controls the next action:
- `memory_issue`: mark the memory as `needs_revision`; route to senior for rewrite or supplementation before more weak-agent replay
- `agent_capability`: keep the memory usable, but adjust routing away from that model for this task type
- `task_mismatch`: create or choose a different replay memory
- `environment_issue`: fix runtime, files, dependencies, auth, quota, or network before retry
- `unknown`: leader reviews evidence before retrying

## Branch Card Schema

Create a branch card before dispatch. Prefer `costmarshal.py new-task` over manual creation.

```yaml
id: CM-001
title: short stable title
agent_tier: high|medium|low
preferred_agent: senior|deepseek|kimi|longcat|auto
difficulty: S|A|B|C
risk: high|medium|low
task_type: architecture|implementation|analysis|review|mechanical|summarization|verification
purpose: one sentence
allowed_context:
  - master-snapshot.md
  - explicit/path.ext
replay_memory:
  - memory/replay/mechanical/reusable-flow/memory.md
depends_on:
  - CM-0001
forbidden_context:
  - raw transcripts from other workers unless explicitly approved
write_scope:
  mode: none|single_file|disjoint_files|coupled_files|unknown
  allowed_paths: []
claimed_paths:
  - src/module.py
forbidden_actions:
  - do not edit files outside allowed_paths
  - do not expose or request raw API keys
expected_artifacts:
  - status.json
  - completion-report.md
budget:
  max_wall_minutes: 20
  max_input_tokens: 50000
  max_output_tokens: 4000
  max_cost_cny: 0.50
success_signal: DONE file or status.json state=done
escalate_if:
  - low confidence
  - tests fail
  - needs broader context
```

## Worker Brief Schema

Give workers a brief, not the leader transcript.

```markdown
# Task CM-001

Purpose:

Acceptance criteria:

Allowed context:

Replay memory:

Allowed writes:

Commands allowed:

Commands forbidden:

Budget:

Return protocol:
- Write status.json.
- Write completion-report.md.
- Create DONE, FAILED, or ESCALATE.

Escalate instead of guessing when:
```

## Dependencies, Locks, Handoff, And Review

Use dependencies to prevent premature execution:

```bash
python scripts/costmarshal.py new-task --project <project-dir> --title "Replay" --purpose "..." --agent longcat --depends-on CM-0001
```

`set-status --state running` rejects tasks whose dependencies are not `done`.

Use write claims to prevent two active agents from editing overlapping paths:

```bash
python scripts/costmarshal.py new-task --project <project-dir> --title "Patch config" --purpose "..." --agent kimi --claim-path configs/eval.yaml --allowed-path configs/eval.yaml
```

Overlapping claims are rejected unless the new task depends on the current claimant or the leader explicitly uses `--allow-lock-conflict`.

Use compressed handoff files instead of raw transcripts:

```bash
python scripts/costmarshal.py record-handoff --project <project-dir> --source-task CM-0001 --summary "What the next agent needs" --next-step "Review evidence"
```

Use bounded review tasks for cross-agent checking:

```bash
python scripts/costmarshal.py new-review-task --project <project-dir> --source-task CM-0001 --reviewer kimi
```

Use `status-project` during long runs:

```bash
python scripts/costmarshal.py status-project --project <project-dir>
```

## Read-Only Worker Runner

Use `run-task` for OpenAI-compatible medium and low-tier workers when the task can be handled from `brief.md` and approved context:

```bash
python scripts/costmarshal.py run-task --project <project-dir> --task CM-0001 --dry-run
python scripts/costmarshal.py run-task --project <project-dir> --task CM-0001
```

The runner:
- loads local secret files without printing key values
- reads `brief.md` and branch-card `allowed_context`
- skips `raw/` context unless `--allow-raw-context` is explicit
- writes `raw/worker-output.md` and `raw/worker-response.json`
- writes `completion-report.md`
- records provider input/output token usage and estimated CNY cost
- marks the task terminal through the normal status files
- keeps usage separate from leader evaluation; run `record-result` after review to update quality and acceptance

The runner is read-only. It must not be used as proof that code was changed. For implementation tasks, treat its output as a patch plan or review unless another executor applies changes.

## Embedded Wait Protocol

Use CostMarshal's embedded WakeWait-style commands instead of requiring a separate WakeWait skill:

```bash
python scripts/costmarshal.py sleep --duration 10m
python scripts/costmarshal.py wait-task --project <project-dir> --task CM-0001 --every 30s --timeout 1h
python scripts/costmarshal.py wait-file --path <path> --every 30s --timeout 1h
python scripts/costmarshal.py wait-contains --path <log> --text DONE --every 30s --timeout 1h
python scripts/costmarshal.py wait-command --command "<check command>" --every 30s --timeout 1h
```

The bundled compatibility scripts are available at `scripts/wakewait.ps1` and `scripts/wakewait.sh`.

## Status Schema

Use JSON for machine-readable state.

```json
{
  "task_id": "CM-001",
  "agent": "kimi",
  "state": "running",
  "started_at": "2026-07-08T00:00:00+08:00",
  "updated_at": "2026-07-08T00:10:00+08:00",
  "confidence": "medium",
  "summary_path": "completion-report.md",
  "artifacts": [],
  "error": null,
  "needs_escalation": false
}
```

Allowed states: `planned`, `running`, `done`, `failed`, `escalate`, `cancelled`.

## Completion Report Schema

The leader reads this first.

```markdown
# Completion Report: CM-001

Status: done|failed|escalate
Agent:
Task type:
Confidence: high|medium|low

## Result
One concise paragraph.

## Evidence
- Tests run:
- Files changed:
- Artifacts created:
- Log markers:

## Budget
- Wall time:
- Input tokens:
- Output tokens:
- Estimated cost CNY:

## Replay Memory Feedback
- Memory files used:
- Was the memory sufficient: yes|partial|no
- Memory quality score: 1-5
- Missing or ambiguous details:
- Suggested memory improvements:
- Failure attribution: memory_issue|agent_capability|task_mismatch|environment_issue|unknown

## Decisions Needed From Leader
- none, or list exact decision

## Escalation Reason
- only if status is failed or escalate

## Suggested Merge Note
Short text safe to merge into leader context.
```

## Scorecard Schema

Append one JSONL row per task. Prefer `costmarshal.py record-result` so project and global memory stay synchronized.

```json
{
  "timestamp": "2026-07-08T00:00:00+08:00",
  "task_id": "CM-001",
  "agent": "deepseek",
  "model_tier_prior": "medium",
  "task_type": "analysis",
  "difficulty": "A",
  "risk": "medium",
  "context_size": "medium",
  "wall_seconds": 420,
  "input_tokens": 120000,
  "output_tokens": 30000,
  "total_tokens": 150000,
  "estimated_cost_cny": 0.18,
  "cost_source": "auto_pricing_cny_per_1m",
  "completed": true,
  "needs_escalation": false,
  "accepted_by_leader": true,
  "accepted_by_senior": null,
  "test_result": "passed",
  "rework_count": 0,
  "failure_type": null,
  "quality_score": 4
}
```

Quality score:
- 5: accepted with no edits and strong evidence
- 4: accepted with minor edits
- 3: useful but needed rework
- 2: incomplete or weak evidence
- 1: wrong, unsafe, or misleading

Token/cost fields:
- `input_tokens`: provider-reported prompt/input tokens when available, otherwise the leader's best estimate.
- `output_tokens`: provider-reported completion/output tokens when available, otherwise the leader's best estimate.
- `total_tokens`: generated by `record-result` from input plus output tokens.
- `estimated_cost_cny`: caller-provided value, or auto-estimated from configured per-agent CNY-per-1M-token prices when token counts are supplied.
- `cost_source`: `caller`, `auto_pricing_cny_per_1m`, `missing_pricing`, or `not_provided`.

## Budget Policy

Set a budget on every task. Include wall time, max output, and rough money cap when using external paid models.

New projects default to `--max-project-cost-cny 20`. `new-task` rejects a planned task when its `--max-cost-cny` would exceed the remaining project budget. Use `new-project --max-project-cost-cny <amount>` only when the run intentionally needs more.

Use short outputs for medium-tier models unless the task requires details. Use LongCat for cheap repeated execution, but do not let low cost bypass verification.

Stop or escalate on:
- budget exceeded
- repeated tool/API error
- rate limit without near-term recovery
- uncertainty about allowed writes
- missing verification path

## Acceptance Flow

1. Confirm `status.json` is `done`, or inspect failure/escalation.
2. Read `completion-report.md`.
3. Verify required artifacts and tests.
4. If code changed, review diff and run relevant checks.
5. If replay memory was used, record memory feedback and inspect attribution.
6. If feedback says `memory_issue` or needs senior refresh, stop weak replay and route memory revision to senior.
7. If feedback says `agent_capability`, keep the memory but adjust model routing.
8. If evidence is weak, request rework or escalate.
9. Merge only the suggested merge note or a leader-compressed summary.
10. Update scorecard.
11. At project completion, run `finish-project` or `evolve-project` so successful paths become routing evidence and compact knowledge lessons.

## Script Commands

Use these commands as the stable interface:

```bash
python scripts/costmarshal.py init-root
python scripts/costmarshal.py new-project --kind arbor --name run-name --objective "..." --max-project-cost-cny 20
python scripts/costmarshal.py adopt-project --path <existing-project> --kind arbor --name adopted-run --objective "..."
python scripts/costmarshal.py check-agents --project <project-dir>
python scripts/costmarshal.py draft-plan --project <project-dir> --summary "..." --step "..." --task "..." --agent-plan "..." --predicted-cost-cny 3 --predicted-wall-time "30m" --acceptance "..." --verification "..." --risk "..."
python scripts/costmarshal.py approve-plan --project <project-dir> --approved-by user
python scripts/costmarshal.py recommend --task-type implementation --difficulty A --risk medium
python scripts/costmarshal.py new-task --project <project-dir> --title "..." --purpose "..." --agent kimi --difficulty A --risk medium --task-type implementation --claim-path src/module.py
python scripts/costmarshal.py run-task --project <project-dir> --task CM-0001 --dry-run
python scripts/costmarshal.py run-task --project <project-dir> --task CM-0001
python scripts/costmarshal.py record-handoff --project <project-dir> --source-task CM-0001 --summary "Compressed handoff"
python scripts/costmarshal.py new-review-task --project <project-dir> --source-task CM-0001 --reviewer kimi
python scripts/costmarshal.py promote-memory --project <project-dir> --source-task CM-0001 --name reusable-flow --memory-task-type mechanical --summary "Exact replayable procedure" --working-dir "." --required-input "config.yaml exists" --allowed-param "top_k" --allowed-command "python run_eval.py --config config.yaml" --expected-output "results.json" --success-marker "command exits 0"
python scripts/costmarshal.py new-task --project <project-dir> --title "..." --purpose "Replay proven flow" --agent longcat --difficulty B --risk low --task-type mechanical --replay-memory reusable-flow --depends-on CM-0001
python scripts/costmarshal.py record-memory-feedback --project <project-dir> --task CM-0003 --outcome succeeded --sufficient yes --memory-quality 5 --attribution unknown
python scripts/costmarshal.py status-project --project <project-dir>
python scripts/costmarshal.py set-status --project <project-dir> --task CM-0001 --state running
python scripts/costmarshal.py record-result --project <project-dir> --task CM-0001 --agent kimi --model kimi-k2.7-code --status done --quality-score 4 --accepted-by-leader
python scripts/costmarshal.py finish-project --project <project-dir>
python scripts/costmarshal.py evolve-project --project <project-dir>
python scripts/costmarshal.py validate --project <project-dir>
```

Use `--root <dir>` or `COSTMARSHAL_HOME` when the global storage root must be explicit. Legacy `MMC_HOME` is still accepted.
