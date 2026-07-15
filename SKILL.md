---
name: costmarshal
description: "CostMarshal v2.3: scheduler-first, cost-aware low/medium/high provider orchestration for Codex CLI with conservative route optimization, durable attempts, budget reservations, isolated worker worktrees, recovery, leader acceptance, and optional ArchMarshal governance checks."
---

# CostMarshal v2.3

Use this skill for long or decomposable work where multiple API price/capability tiers should cooperate under explicit safety, cost, and recovery controls.

Only `scripts/costmarshal.py` and the `costmarshal_v2` package are official. Do not use legacy `scripts/mc.py` flows.

## Invariants

1. The scheduler relays commands and supervises processes; it does not make technical decisions.
2. Provider identity and tier are separate. Tiers are exactly `low`, `medium`, and `high`.
3. Risk/difficulty/task-type safety floors cannot be bypassed by a cheaper provider request.
4. A task is done only after an explicit leader result with `accepted_by_leader=true`.
5. Workers may return only `waiting_leader`, `failed`, or `escalate` through collect.
6. Every worker command is fenced to its actor and attempt; stale commands must not mutate the current attempt.
7. Budgeted dispatch requires reviewed pricing and non-zero token estimates.
8. Workers never receive the CostMarshal runtime as a writable directory.
9. Writable worker tasks use isolated git worktrees and a post-run path gate.
10. Low/medium workers receive only their selected provider credential and a credential-free actor Codex home.
11. ArchMarshal integration is read-only. Never auto-adopt, apply, start, end, or modify ArchMarshal.

## Standard workflow

1. Confirm the writable workspace, provider catalog, budget, and governance mode.
2. Configure required Codex profiles with `configure-provider`; never store API keys in profile files.
3. Initialize the project.
4. Create bounded tasks with explicit risk, difficulty, estimates, acceptance criteria, allowed context, and write scope.
5. Run `route` to inspect safety floor, chain, cost, and acceptance prior when economics matter.
6. Dispatch only after the route explanation and claims are acceptable.
7. Keep `run-scheduler` active while actors execute.
8. Review the completion report, isolated worktree diff, tests, and evidence.
9. Record the leader result. Escalate one tier when evidence is insufficient.
10. Run `validate`, and use `recover` after an unclean stop.

## Commands

```powershell
# Configure an OpenAI-compatible provider profile.
python scripts/costmarshal.py configure-provider --codex-home "$env:CODEX_HOME" --profile medium-api --provider-id medium-api --base-url "https://reviewed-endpoint/v1" --model "reviewed-model" --env-key MEDIUM_API_KEY

# Initialize three-tier routing.
python scripts/costmarshal.py init --name <name> --objective "<objective>" --workspace <workspace> --provider-catalog <catalog.json> --project-budget-cny <amount> --governance off

# Create a bounded task.
python scripts/costmarshal.py new-task --project <project-dir> --title "<title>" --purpose "<purpose>" --task-type implementation --risk medium --difficulty normal --estimated-input-tokens 100000 --estimated-output-tokens 10000 --claim-path src/file.py --allowed-path src/file.py

# Explain without mutation.
python scripts/costmarshal.py route --project <project-dir> --task-type implementation --risk medium --difficulty normal --estimated-input-tokens 100000 --estimated-output-tokens 10000

# Dispatch and supervise.
python scripts/costmarshal.py dispatch --project <project-dir> --task V2-0001 --start
python scripts/costmarshal.py run-scheduler --project <project-dir>

# Accept only after leader review.
python scripts/costmarshal.py record-result --project <project-dir> --task V2-0001 --attempt <attempt-id> --status done --quality-score 5 --accepted-by-leader

# Inspect and recover.
python scripts/costmarshal.py dashboard --project <project-dir>
python scripts/costmarshal.py providers --project <project-dir>
python scripts/costmarshal.py budget --project <project-dir>
python scripts/costmarshal.py governance-status --project <project-dir>
python scripts/costmarshal.py validate --project <project-dir>
python scripts/costmarshal.py recover --project <project-dir> --plan-restarts
```

## Routing policy

- High risk or hard difficulty: high floor.
- Medium risk or implementation/review/code-review: medium floor.
- Low-risk bounded analysis, docs, extraction, mechanical work, small edits, summaries, tests, or verification: low floor.
- Unknown or judgment-heavy types: medium floor.

With complete reviewed prices and token estimates, compare all valid provider combinations in escalation chains using expected cost per leader-accepted result. Enforce `--min-success-probability` when supplied. Without complete inputs, choose the minimum safe available tier. Explain the fallback; do not manufacture a price.

## Provider catalog

Treat pricing as reviewed configuration. Use `null` for unknown values. A provider entry contains:

- `provider_id`, `tier`, `profile`, `model`, `env_key`
- `enabled`, `priority`
- `input_cny_per_1m`, `output_cny_per_1m`
- `capabilities`

Task `--require-capability` values are hard constraints: providers lacking every required capability are removed before tier and cost selection.

## Worker write policy

- No write paths: source workspace is read-only.
- Write paths: source workspace must be a clean git repository root; execution occurs in a detached runtime worktree.
- After exit, changed/untracked paths must all be within the declared write scope.
- The leader reviews and integrates isolated changes.
- Never permit claims for `.agent`, `.agents`, `AGENTS.md`, `AGENTS.override.md`, `.git`, `.codex`, or `.codex-plugin`.
- Custom worker command templates are disabled by default because they bypass the controlled runner. The explicit `--allow-unsafe-custom-worker-commands` compatibility escape hatch forfeits sandbox and secret-isolation guarantees and must not be enabled for untrusted work.

## ArchMarshal governance

Use `--governance required --archmarshal-wrapper <exact invoke_archmarshal.py>` only after the workspace is explicitly managed by ArchMarshal. CostMarshal stores a binding fingerprint and validates it before dispatch and actor launch. A drift blocks execution.

If adoption or repair is required, stop CostMarshal work and use the ArchMarshal skill/workflow explicitly with preview-first safety. Do not infer permission to apply an ArchMarshal plan.

## Verification before handoff

Run every command listed in README's Verification section. At minimum, compile plus routing, model rotation, security, actor security, reliability, budget, concurrency, backend, ArchMarshal compatibility, and install smoke tests must pass.

## Known beta boundary

The JSON/JSONL control plane is protected by an OS writer lock, a scheduler singleton lock, and idempotency fences, but multi-file dispatch/escalation is not fully crash-atomic. Do not describe v2.3-beta as transactionally exactly-once. After a hard crash, run `validate` and `recover`; a future non-beta release should move the control plane to a transactional store and strengthen the worker filesystem read boundary.
