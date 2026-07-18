---
name: costmarshal
description: "CostMarshal v3.0 internal policy/runtime for the Codex plugin: scheduler-first, cost-aware low/medium/high provider orchestration with per-step cache-safe pricing, completion-first routing, recoverable effects, OCI worker isolation, durable attempts, budget reservations, leader acceptance, and optional read-only ArchMarshal governance. Invoke this legacy root Skill explicitly only; normal Codex use enters through orchestrate-cost-aware-agents."
---

# CostMarshal v3.0

Use this skill for long or decomposable work where multiple API price/capability tiers should cooperate under explicit safety, cost, and recovery controls.

Only `scripts/costmarshal.py` and the `costmarshal_v2` package are official. Do not use legacy `scripts/mc.py` flows.

## Invariants

1. The scheduler relays commands and supervises processes; it does not make technical decisions.
2. Provider identity and tier are separate. Tiers are exactly `low`, `medium`, and `high`.
3. Risk/difficulty/task-type safety floors cannot be bypassed by a cheaper provider request.
4. A task is done only after an explicit leader result with `accepted_by_leader=true`.
5. Workers may report failed/escalate evidence, but actor-authored collect commands may request only `waiting_leader`; task outcome and continuation are leader-owned.
6. Every worker command is fenced to its actor and attempt; stale commands must not mutate the current attempt.
7. Budgeted dispatch requires reviewed pricing and non-zero token estimates.
8. Production workers require an attested OCI boundary; required mode never falls back to native execution.
9. Unsafe native workers require both project-level and dispatch-level explicit opt-in and must be treated as unisolated.
10. Every attempt has a launch token and lifetime runtime lock; a duplicate or outcome-unknown runner must not call the provider.
11. SQLite cutover is explicit and marker-driven. A present but invalid marker fails closed; JSON/JSONL are compatibility views after cutover.
12. ArchMarshal integration is read-only. Never auto-adopt, apply, start, end, or modify ArchMarshal.
13. Every admitted provider profile is an exact-byte SHA-256 snapshot bound to the route, attempt, runtime effect, and OCI identity; launch/recovery never re-resolves mutable profile source bytes.
14. Every provider attempt is collected before continuation. A worker never authorizes more spend; a sealed required attempt with an admitted successor needs an explicit leader rejection with a bounded handoff before the next tier can start. A terminal rejection without a handoff cannot later continue that sealed route.

## Standard workflow

1. Confirm the writable workspace, provider catalog, budget, and governance mode.
2. Configure required Codex profiles with `configure-provider`; never store API keys in profile files.
3. Initialize the project.
4. Create bounded tasks with explicit risk, difficulty, estimates, acceptance criteria, allowed context, and write scope.
5. Run `route` to inspect safety floor, chain, cost, and acceptance prior when economics matter.
6. Dispatch only after the route explanation and claims are acceptable.
7. Keep `run-scheduler` active while actors execute.
8. Review the completion report, tests, and evidence. For a sealed write output, run `preview-changes` before acceptance; it must not modify the source workspace.
9. Record the leader result. When sealed evidence is insufficient, reject it with a bounded `--handoff`, then explicitly continue to the exact next distinct provider in the admitted non-decreasing chain; that step may be a sealed same-tier peer or may skip a tier.
10. After accepting reviewed changes, run `apply-changes` once to obtain the hash-bound contract, then repeat with `--apply --preview-sha ... --command-id ...`. The command stages but never commits the exact candidate tree. After SQLite cutover, preview and explicit apply use owner-leased recoverable Git effects; a command may honestly report `queued` when another drainer owns the effect fence.
11. Run `validate`, and use `recover` after an unclean stop.

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
python scripts/costmarshal.py record-result --command-id CMD-RESULT-ACCEPT-001 --project <project-dir> --task V2-0001 --attempt <attempt-id> --status done --quality-score 5 --accepted-by-leader

# Reject a sealed required attempt and bind the exact successor handoff.
python scripts/costmarshal.py record-result --command-id CMD-RESULT-REJECT-001 --project <project-dir> --task V2-0001 --attempt <attempt-id> --status escalate --quality-score 2 --handoff "Bounded findings, failed checks, and the exact remaining decision."
python scripts/costmarshal.py escalate --project <project-dir> --task V2-0001 --reason "Leader rejected the current result" --start

# For a sealed write result, preview before acceptance; explicitly apply after acceptance.
python scripts/costmarshal.py preview-changes --command-id CMD-CHANGE-PREVIEW-001 --project <project-dir> --task V2-0001 --attempt <attempt-id>
python scripts/costmarshal.py apply-changes --project <project-dir> --task V2-0001 --attempt <attempt-id>
python scripts/costmarshal.py apply-changes --project <project-dir> --task V2-0001 --attempt <attempt-id> --apply --preview-sha <sha256:...> --command-id CMD-CHANGE-APPLY-001

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

With complete reviewed prices and token estimates, exhaustively compare every safe non-decreasing chain of one to three distinct provider IDs, including mature same-tier peer continuations, early-stop, and tier-skip plans, using expected cost per leader-accepted result. Provider repetition and tier downgrade are always illegal. New projects default to `completion-first`: compare only plans whose terminal fallback reaches the strongest compatible enabled tier, but execute successors only after explicit leader rejection so earlier acceptance stops spend. `cost-only` is an explicit project/task opt-out; legacy projects missing the field remain cost-only. For automatic routes, reject more than 16 enabled compatible providers in any tier at or above the effective safety floor; providers below the floor cannot enter the plan and do not count. Explicit provider selection and explicit-tier ranking bypass this combinatorial cap. Enforce the task `--min-success-probability` when supplied, otherwise freeze the optional project default. Without a positive floor, if the complete chain across all available safe tiers has fewer than 10 exact task/profile/conditional-lineage observations at a continuation, seal the most cost-efficient one-provider-per-tier chain in `conditional-evidence-bootstrap`. Do not invent a continuation probability or add an exploratory same-tier call. After lineages mature, return to the frozen objective. A same-tier successor is legal only as the exact next distinct provider in the active sealed envelope. Before first dispatch, bind each step to its exact provider/model/profile/profile-hash execution identity, its own token forecast, price basis, and acceptance evidence. A cached-input discount requires a separately proven complete origin; when the origin is absent or identity changes, reclassify source cached input as ordinary input. Reserve the sum of all step estimates in the v3 task envelope; continuations consume the immutable step forecast without double counting or mutable config reads. Without complete economic inputs, choose the minimum safe available tier and explain the fallback.

Each admitted acceptance prior binds the exact trusted result IDs and a canonical
digest of those rows. Before a continuation spends the next tier, require every
bound row to remain trusted and byte-equivalent; unrelated newly trusted rows do
not invalidate the frozen plan.

A non-zero success floor needs at least 10 trusted v3 observations at every exact
task/profile/conditional lineage position. Cold-start projects therefore fail
closed unless the reviewed task explicitly uses `0`. v2 result rows are retained
only as historical audit data and never train v3 routing. Describe this as a
conservative history-based acceptance floor, not a formal availability SLA or a
multiple-comparison-adjusted guarantee.

Budget admission/reconciliation uses integer nano-CNY internally and rejects values with more than 9 decimal places. Decode monetary JSON without binary-float conversion, multiply exact reviewed rates by integer step tokens, and round any fractional reservation upward. Reprice cumulative usage once from the immutable attempt snapshot. An explicit all-zero final provider usage observation settles a reviewed `fixed_attempt`; missing usage remains unsettled. A caller-priced/unverified row is sticky and cannot later be washed clean. Budget envelopes are accounting controls over estimates, not external hard-spend guarantees; claim a hard cap only when the provider proxy enforces one.

## Provider catalog

Treat pricing as reviewed configuration. Use `null` for unknown values. A provider entry contains:

- `provider_id`, `tier`, `profile`, `model`, `env_key`
- `enabled`, `priority`
- canonical `pricing`: `currency`, `source`, `reviewed_at`, `effective_at`,
  `expires_at`, `snapshot_id`, `snapshot_hash`, `input_per_1m`,
  `cached_input_per_1m`, `output_per_1m`, and `fixed_attempt`. Per-wire-request
  fixed fees are unsupported because an attempt may issue multiple requests.
  Hash-valid beta snapshots with `fixed_request=0` remain readable; non-zero
  legacy request fees fail closed without request-count metering.
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
- The selected provider credential is inside the worker/provider-client trust domain. Literal redaction is not protection against a malicious model encoding that key. Use only dedicated spend-capped credentials and do not claim hostile-workload credential isolation without an external broker.
- After provider completion, required mode persists project/attempt-scoped, content-addressed report, recursively redacted event, and completion receipts before cleanup. `finished_pending_finalize` recovery may only attach/inspect/cleanup the exact terminal OCI identity and finalize from those bytes; it must never call the provider again.
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

For ordinary installed-plugin work, verify the actual project with the supported
`validate`, read-only `route`, `dashboard`, and `recover` surfaces appropriate to
the request. Do not turn source-release maintenance into an ordinary-user step.

Only when publishing from a full source checkout that contains `README.md`,
`tests/`, and `release/`, run every command in README's Required local
verification section. At minimum, compile plus routing, model rotation,
security, actor security, reliability, budget, concurrency, backend,
ArchMarshal compatibility, install smoke, and machine-receipt runtime tests
must pass. Non-beta release-evidence commands are separate and are expected to
return a machine-readable blocked result when their preregistered external
inputs are absent. The curated installed runtime snapshot intentionally omits
those maintainer-only sources.

## Transaction and certification boundary

An explicit `migrate-state --apply` cutover makes SQLite WAL authoritative for scheduler control state; compatibility views are materialized after commit under a cross-process materializer lock and repaired on restart. Stable command IDs are payload-hashed, while spawn/stop and Git preview/apply I/O are represented by owner-leased effects outside the transaction; slow effects renew their lease and a crashed owner becomes recoverable after expiry. Git-effect payloads freeze the task/attempt/base SHA/write scope/manifest/review contract, execution revalidates authoritative state, and task receipts/events are projected idempotently before the original effect command completes. Do not enable cutover while actors are live.

Do not describe CostMarshal as universally economically optimal or externally certified without the machine-readable real-provider shadow matrix and live malicious OCI evidence for the reviewed digest. Spawn/stop use leased transactional effects and required dispatch uses the OCI snapshot/profile/credential/report adapter; external effects are fenced and recoverable rather than magically exactly-once. Missing external evidence remains an explicit release/certification gate, never a silent fallback.
