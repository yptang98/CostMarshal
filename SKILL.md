---
name: costmarshal
description: "CostMarshal v2.4: scheduler-first, cost-aware low/medium/high provider orchestration for Codex CLI with conservative route optimization, recoverable runtime effects, OCI worker isolation, durable attempts, budget reservations, recovery, leader acceptance, and optional ArchMarshal governance checks."
---

# CostMarshal v2.4

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
8. Production workers require an attested OCI boundary; required mode never falls back to native execution.
9. Unsafe native workers require both project-level and dispatch-level explicit opt-in and must be treated as unisolated.
10. Every attempt has a launch token and lifetime runtime lock; a duplicate or outcome-unknown runner must not call the provider.
11. SQLite cutover is explicit and marker-driven. A present but invalid marker fails closed; JSON/JSONL are compatibility views after cutover.
12. ArchMarshal integration is read-only. Never auto-adopt, apply, start, end, or modify ArchMarshal.
13. Every admitted provider profile is an exact-byte SHA-256 snapshot bound to the route, attempt, runtime effect, and OCI identity; launch/recovery never re-resolves mutable profile source bytes.

## Standard workflow

1. Confirm the writable workspace, provider catalog, budget, and governance mode.
2. Configure required Codex profiles with `configure-provider`; never store API keys in profile files.
3. Initialize the project.
4. Create bounded tasks with explicit risk, difficulty, estimates, acceptance criteria, allowed context, and write scope.
5. Run `route` to inspect safety floor, chain, cost, and acceptance prior when economics matter.
6. Dispatch only after the route explanation and claims are acceptable.
7. Keep `run-scheduler` active while actors execute.
8. Review the completion report, isolated worktree diff, tests, and evidence.
9. Record the leader result. When evidence is insufficient, continue to the next provider step in the admitted monotonic chain; that step may skip a tier.
10. Run `validate`, and use `recover` after an unclean stop.

## Commands

```powershell
# Configure an OpenAI-compatible provider profile.
python scripts/costmarshal.py configure-provider --codex-home "$env:CODEX_HOME" --profile medium-api --provider-id medium-api --base-url "https://reviewed-endpoint/v1" --model "reviewed-model" --env-key MEDIUM_API_KEY

# Initialize three-tier routing.
python scripts/costmarshal.py init --name <name> --objective "<objective>" --workspace <workspace> --provider-catalog <catalog.json> --project-budget-cny <amount> --default-min-success-probability <0..1> --governance off --worker-image <name@sha256:digest>

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
python scripts/costmarshal.py governance-rebind --project <project-dir>
python scripts/costmarshal.py validate --project <project-dir>
python scripts/costmarshal.py recover --project <project-dir> --plan-restarts

# Preview/apply the opt-in transactional authority only with actors stopped.
python scripts/costmarshal.py migrate-state --project <project-dir>
python scripts/costmarshal.py migrate-state --project <project-dir> --apply
python scripts/costmarshal.py state-store --project <project-dir>
```

## Routing policy

- High risk or hard difficulty: high floor.
- Medium risk or implementation/review/code-review: medium floor.
- Low-risk bounded analysis, docs, extraction, mechanical work, small edits, summaries, tests, or verification: low floor.
- Unknown or judgment-heavy types: medium floor.

With complete reviewed prices and token estimates, exhaustively compare every safe monotonic provider subchain, including early-stop and tier-skip plans, using expected cost per leader-accepted result. Reject more than 16 enabled compatible providers in one tier. Enforce the task `--min-success-probability` when supplied, otherwise freeze the optional project `default_min_success_probability` into a new auto-routed task. Without either SLA, explain that collaboration is permitted but not required. Before the first dispatch, bind each selected step to its price basis and exact-byte provider profile identity, then reserve the sum of all step estimates in a task-level admission envelope; never use probability-weighted expected cost as the reservation. Continuations consume that envelope and immutable profile snapshot without double counting or re-reading mutable source config, and drift fails closed. Without complete economic inputs, choose the minimum safe available tier. Explain the fallback; do not manufacture a price.

Budget admission/reconciliation uses integer nano-CNY internally and rejects values with more than 9 decimal places. Decode catalog monetary JSON without binary-float conversion, canonicalize reviewed rates as exact decimal strings, multiply them by integer tokens, and round any fractional nano-CNY reservation upward. Reprice cumulative usage once from the immutable attempt snapshot; a caller-priced/unverified row is sticky even when it reports zero tokens and cannot later be washed clean by a final row. Budget envelopes are admission/accounting controls over token estimates, not external hard-spend guarantees. Do not claim a hard monetary cap unless the selected provider proxy enforces request/token or money ceilings.

## Provider catalog

Treat pricing as reviewed configuration. Use `null` for unknown values. A provider entry contains:

- `provider_id`, `tier`, `profile`, `model`, `env_key`
- `enabled`, `priority`
- canonical `pricing`: `currency`, `source`, `reviewed_at`, `effective_at`,
  `expires_at`, `snapshot_id`, `snapshot_hash`, `input_per_1m`,
  `cached_input_per_1m`, `output_per_1m`, and `fixed_request`
- `capabilities`

Canonical snapshots are hash-bound and must be current at routing time. Expired,
future-effective, future-reviewed, mixed-currency, mixed canonical/legacy, or
non-CNY pricing disables CNY optimization and yields no budget-eligible cost.
The old flat `input_cny_per_1m`/`output_cny_per_1m` fields remain available only
as explicitly labelled beta compatibility; they have no freshness guarantee and
produce no immutable price snapshot.

In legacy JSON authority, a crash-replayed escalation must match the complete
prepared successor admission, including provider/model/profile, env-key
selector, runtime backend, full worker-isolation execution and attestation,
immutable profile identity, route/pricing plan, and budget projection.

Task `--require-capability` values are hard constraints: providers lacking every required capability are removed before tier and cost selection.

## Worker isolation policy

- `required` is the default and may select only Docker/Podman Linux containers.
- Missing engine/image/canary/network support fails before attempt persistence and budget reservation.
- Images must be digest pinned and use pull-never, read-only rootfs, non-root UID, dropped capabilities, no-new-privileges, limits, and explicit mounts.
- Required dispatch uses the attested OCI adapter and bundled digest-buildable worker image; unrestricted bridge networking is forbidden, and missing live engine/image/canary/provider-proxy evidence fails closed.
- OCI start/recovery verifies the exact managed environment contract, and the worker requires the admitted profile SHA-256 before provider execution. Missing profile bindings never fall back to mutable user configuration.
- Development compatibility requires `init --allow-unsafe-native-workers` and `dispatch --unsafe-native`; the attestation records `strong_isolation=false`. Required or ready ArchMarshal governance forbids new native provider launches, so governed work must use required OCI isolation.

## Worker write policy

- No write paths: source workspace is read-only.
- Write paths: source workspace must be a clean git repository root; execution occurs in a detached runtime worktree.
- After exit, changed/untracked paths must all be within the declared write scope.
- The leader reviews and integrates isolated changes.
- Never permit claims for `.agent`, `.agents`, `AGENTS.md`, `AGENTS.override.md`, `.git`, `.codex`, or `.codex-plugin`.
- Custom worker command templates are disabled by default because they bypass the controlled runner. The explicit `--allow-unsafe-custom-worker-commands` compatibility escape hatch forfeits sandbox and secret-isolation guarantees and must not be enabled for untrusted work.

## ArchMarshal governance

Use `--governance required --archmarshal-launcher <exact run_archmarshal.py>` only after the workspace is explicitly managed by ArchMarshal. CostMarshal invokes only bootstrap-status/doctor read checks, binds the canonical launcher and sibling invoke wrapper, and validates that fingerprint before dispatch and actor launch. A drift blocks execution.

When a required binding drifts, new spawn, relay, scheduler, and direct actor entry paths fail closed. An explicit `stop-actor --stop-runtime` remains available as a STOP-only emergency path; reuse its `--command-id` if recovery is needed. It must never drain a pending SPAWN.

After a CostMarshal binding-format upgrade, use `governance-rebind --archmarshal-launcher <exact run_archmarshal.py>` to preview and
add `--apply` to explicitly refresh only the CostMarshal-side
fingerprint. The prior binding remains in project audit history; ArchMarshal stays
read-only.

If adoption or repair is required, stop CostMarshal work and use the ArchMarshal skill/workflow explicitly with preview-first safety. Do not infer permission to apply an ArchMarshal plan.

## Verification before handoff

Run every command in README's Required local verification section. At minimum, compile plus routing, model rotation, security, actor security, reliability, budget, concurrency, backend, ArchMarshal compatibility, install smoke, and machine-receipt runtime tests must pass. Non-beta release-evidence commands are separate and are expected to return a machine-readable blocked result when their preregistered external inputs are absent.

## Transaction and beta boundary

An explicit `migrate-state --apply` cutover makes SQLite WAL authoritative for scheduler control state; compatibility views are materialized after commit under a cross-process materializer lock and repaired on restart. Stable command IDs are payload-hashed, while spawn/stop I/O is represented by owner-leased effects outside the transaction; slow effects renew their lease and a crashed owner becomes recoverable after expiry. Do not enable cutover while actors are live.

Do not describe v2.4-beta as economically optimal or universally production-ready yet. Spawn/stop use a leased transactional effect worker and required dispatch uses the OCI snapshot/profile/credential/report adapter, but external effects are fenced/recoverable rather than magically exactly-once. A non-beta release still requires the machine-readable real-provider shadow matrix and live malicious OCI evidence for the reviewed digest. These are explicit release gates, not silent fallbacks.
