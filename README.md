# CostMarshal

CostMarshal is a scheduler-first, cost-aware multi-provider control plane for Codex CLI. It coordinates low-, medium-, and high-tier APIs, keeps task and attempt state durable, and reserves the strongest provider for work that needs it.

Current version: `v2.4.0-beta`

> v2.4 adds recoverable spawn/stop effects and an enabled digest-pinned OCI worker path on top of real low/medium/high routing. It does not claim that every deployment is economically optimal by default: reviewed prices, token estimates, leader acceptance history, and the release evidence gates are still required.

## What v2.4 does

- Routes bounded tasks across three capability tiers: low, medium, and high.
- Keeps provider identity separate from capability tier, so providers can be replaced without changing policy.
- Applies a fail-closed safety floor from risk, difficulty, and task type.
- Filters providers by explicit required capabilities before cost optimization.
- Compares priced execution chains such as `low -> medium -> high`, `medium -> high`, and `high` by expected cost per leader-accepted result.
- Escalates one tier at a time and fences stale actor attempts.
- Uses durable actors, mailboxes, reports, claims, usage records, and recovery state.
- Reserves task/project budget at dispatch and settles it from usage or leader results.
- Requires an attested OCI boundary for production workers and never silently falls back to a native process.
- Provides an explicit SQLite WAL cutover for crash-atomic control state, leased runtime effects, and recoverable compatibility views.
- Supports ArchMarshal governance through explicit, read-only binding checks. It never runs ArchMarshal adopt/apply/lifecycle mutations automatically.

## Routing model

The safety floor always wins:

| Task | Minimum tier |
| --- | --- |
| High risk or hard difficulty | high |
| Medium risk, implementation, review, or code review | medium |
| Low-risk bounded analysis, extraction, docs, tests, verification, or small edits | low |
| Unknown or judgment-heavy task type | medium |

When all enabled providers have reviewed prices and the task includes non-zero token estimates, CostMarshal evaluates each valid escalation chain:

```text
expected_chain_cost = C1 + (1-P1)C2 + (1-P1)(1-P2)C3
success_probability = 1 - product(1-Pi)
objective = expected_chain_cost / success_probability
```

`Pi` is a conservative probability derived only from explicit leader acceptance records. If pricing or token estimates are missing, routing falls back to the minimum safe tier rather than inventing a cost.

Use `--min-success-probability 0..1` to impose an SLA floor on priced chains. Routing fails closed when no chain meets it.

Use the read-only route explanation command before dispatch:

```powershell
python scripts/costmarshal.py route `
  --project <project-dir> `
  --task-type analysis `
  --risk low `
  --difficulty normal `
  --estimated-input-tokens 120000 `
  --estimated-output-tokens 10000
```

Operational inspection is also read-only:

```powershell
python scripts/costmarshal.py providers --project <project-dir>
python scripts/costmarshal.py budget --project <project-dir>
python scripts/costmarshal.py governance-status --project <project-dir>
```

## Install

Use [`INSTALL_PROMPT.md`](INSTALL_PROMPT.md) for both first install and update. It resolves
`$CODEX_HOME` with a `~/.codex` fallback, backs up an existing skill, filters
Git metadata, generated evidence, runtime state, bytecode, and secrets, and
leaves both current and legacy runtime roots untouched. Restart Codex if its
skill list is cached, then run the installed smoke test.

```powershell
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
python "$CodexHome\skills\costmarshal\scripts\install_smoke_test.py"
```

CostMarshal uses only Python's standard library at runtime. `git` is required for writable worker worktrees. `tmux` is optional; Windows defaults to the local process backend.

Production worker isolation additionally requires Docker or Podman running Linux containers and a digest-pinned CostMarshal worker image. Native workers are a development compatibility mode, not a security boundary.

## Provider setup

The default catalog contains:

| Provider ID | Tier | Default profile | Credential variable |
| --- | --- | --- | --- |
| `longcat` | low | `longcat` | `LONGCAT_API_KEY` |
| `deepseek` | medium | `deepseek` | `DEEPSEEK_API_KEY` |
| `codex` | high | built-in Codex provider | `CODEX_API_KEY` for each isolated `codex exec` |

Create the legacy LongCat profile:

```powershell
python scripts/costmarshal.py configure-profiles --codex-home "$env:CODEX_HOME"
```

Create any OpenAI-compatible provider profile without storing its key:

```powershell
python scripts/costmarshal.py configure-provider `
  --codex-home "$env:CODEX_HOME" `
  --profile deepseek `
  --provider-id deepseek `
  --display-name "DeepSeek" `
  --base-url "https://your-reviewed-provider-endpoint/v1" `
  --model "your-reviewed-model" `
  --env-key DEEPSEEK_API_KEY
```

Provide credentials through the process environment or a secrets file outside the actor workspace. CostMarshal injects only the selected provider key into that actor and creates a credential-free actor-specific `CODEX_HOME` containing only its profile. New required-mode projects use `CODEX_API_KEY` for the high tier; persisted host login state such as `auth.json` is never mounted into the OCI worker.

## Reviewed provider catalog

Create a JSON file and pass it to `init --provider-catalog`. Non-beta economic
routing uses a hash-bound `pricing` snapshot on every enabled provider. The
snapshot records `currency`, provenance `source`, `reviewed_at`, `effective_at`,
`expires_at`, `snapshot_id`, `snapshot_hash`, ordinary/cached input rates,
output rate, and a fixed request fee. Use
`costmarshal_v2.routing.build_pricing_snapshot(...)` to canonicalize timestamps
and generate the SHA-256 hash; never hand-edit a hash after review.

Expired, future-reviewed, future-effective, mixed-currency, mixed
canonical/legacy, or non-CNY snapshots cannot produce a CNY estimate. Routing
falls back explicitly to the safe tier, and a budgeted dispatch fails closed
because no eligible estimate exists.

Generate a complete new-project catalog rather than hand-writing snapshot
hashes. Review the provider endpoints/profiles separately, then update the
timestamps, sources, IDs, and rates below before writing `providers.json`:

```python
import json
from pathlib import Path
from costmarshal_v2.routing import build_pricing_snapshot, default_provider_catalog

catalog = default_provider_catalog()
rates = {
    "longcat":  (0.8, 0.2, 2.0),
    "deepseek": (2.0, 0.5, 8.0),
    "codex":    (12.0, 3.0, 48.0),
}
for provider in catalog["providers"]:
    input_rate, cached_rate, output_rate = rates[provider["provider_id"]]
    provider["pricing"] = build_pricing_snapshot(
        currency="CNY",
        source=f"https://reviewed.example/{provider['provider_id']}/pricing",
        reviewed_at="2026-07-16T00:00:00Z",
        effective_at="2026-07-16T00:00:00Z",
        expires_at="2026-08-16T00:00:00Z",
        snapshot_id=f"{provider['provider_id']}-2026-07",
        input_per_1m=input_rate,
        cached_input_per_1m=cached_rate,
        output_per_1m=output_rate,
        fixed_request=0.0,
    )
Path("providers.json").write_text(
    json.dumps(catalog, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
```

This preserves the required `CODEX_API_KEY` high-tier contract. Flat price
fields remain readable only for migration of existing pre-v2.4 beta projects;
they are not a valid onboarding example for new budgeted deployments. Unknown
fields, duplicate IDs, invalid tiers, negative prices, malformed environment
keys, and explicitly malformed catalogs are rejected.

## Start a project

```powershell
python scripts/costmarshal.py init `
  --name my-project `
  --objective "Ship the feature with bounded cost" `
  --workspace C:\work\my-project `
  --provider-catalog C:\config\providers.json `
  --project-budget-cny 30 `
  --governance off `
  --worker-image ghcr.io/example/costmarshal-worker@sha256:<reviewed-digest>
```

Create, inspect, and dispatch a task:

```powershell
python scripts/costmarshal.py new-task `
  --project <project-dir> `
  --title "Implement parser" `
  --purpose "Implement and test the bounded parser change" `
  --task-type implementation `
  --risk medium `
  --difficulty normal `
  --estimated-input-tokens 100000 `
  --estimated-output-tokens 12000 `
  --claim-path src/parser.py `
  --allowed-path src/parser.py

python scripts/costmarshal.py dispatch --project <project-dir> --task V2-0001 --start
python scripts/costmarshal.py run-scheduler --project <project-dir>
python scripts/costmarshal.py dashboard --project <project-dir>
```

Worker dispatch defaults to `worker_isolation.mode=required`. Docker/Podman selection may fail over only between supported OCI engines; it never falls back to a host process. Preflight rejects missing daemons, remote contexts, non-Linux engines, unpinned images, unsafe mounts, failed canaries, unrestricted bridge networking, and unsupported network policy before an attempt or budget reservation is persisted.

The required path now supervises the attested container through the bundled execution adapter: prompt over stdin, at most one selected credential file, a credential-free provider profile, bounded JSONL, strict `final.md` exchange validation, deterministic container labels, timeout/stop/cleanup, and a path-free credential deletion receipt. The reviewed image is built from [`container/worker`](container/worker/README.md); both the base image digest and Codex CLI version are mandatory build inputs, and dispatch still requires the resulting immutable image digest.

Production API access uses an externally provisioned `provider-proxy` topology:
the worker attaches only to an internal, CostMarshal-labelled network, while a
separately reviewed proxy is dual-homed to that network and an egress network.
The live evidence harness requires `COSTMARSHAL_OCI_IMAGE`,
`COSTMARSHAL_OCI_PROVIDER_NETWORK`, `COSTMARSHAL_OCI_PROXY_CONTAINER`,
`COSTMARSHAL_OCI_PROXY_HEALTH_URL`, and `COSTMARSHAL_OCI_PROXY_HEALTH_SHA256`.
It verifies the running proxy, an independently inspected non-internal egress
network, immutable identities, and a bounded credential-free allowlisted
response reached through the proxy rather than trusting topology alone.

For trusted development tests only, both project initialization and each dispatch may instead opt in to the unisolated host path:

```powershell
python scripts/costmarshal.py init ... --allow-unsafe-native-workers
python scripts/costmarshal.py dispatch --project <project-dir> --task V2-0001 --unsafe-native --start
```

This records `strong_isolation=false`; it is forbidden when ArchMarshal governance is required. It does not protect host files from the model process.

Custom worker command templates are rejected by default because they bypass the controlled runner. `init --allow-unsafe-custom-worker-commands` exists only as an explicit compatibility escape hatch for trusted legacy tests/backends; it forfeits sandbox and secret-isolation guarantees and cannot be recoverably started after SQLite cutover.

## Leader acceptance

Workers cannot mark a task done. They can only return `waiting_leader`, `failed`, or `escalate`. Completion requires an explicit accepted leader result:

```powershell
python scripts/costmarshal.py record-result `
  --project <project-dir> `
  --task V2-0001 `
  --attempt <attempt-id> `
  --status done `
  --quality-score 5 `
  --accepted-by-leader
```

Only these explicit records train provider acceptance priors.

If the leader rejects an attempt but wants the reviewed chain to continue, record the
decision and then explicitly queue the next stronger tier. `record-result` does not
silently spend more budget:

```powershell
python scripts/costmarshal.py record-result --project <project-dir> --task V2-0001 --attempt <attempt-id> --status escalate --quality-score 2
python scripts/costmarshal.py escalate --project <project-dir> --task V2-0001 --reason "Leader rejected the current result" --start
python scripts/costmarshal.py run-scheduler --project <project-dir> --once
```

## Budget behavior

- A budgeted dispatch requires non-zero token estimates and reviewed prices.
- Dispatch reserves the planned attempt cost while holding the project writer lock.
- Active reservations count against both task and project budgets.
- Usage accumulates actual cost; a leader result settles any remaining estimate.
- Replayed mailbox commands are deduplicated by message ID for task creation, dispatch, escalation, collection, usage, and results.

## ArchMarshal compatibility

CostMarshal can be managed alongside [ArchMarshal](https://github.com/yptang98/ArchMarshal) without giving workers control of governance files.

```powershell
python scripts/costmarshal.py init `
  --objective "Governed project" `
  --workspace C:\work\project `
  --governance required `
  --archmarshal-wrapper C:\path\to\ArchMarshal\scripts\invoke_archmarshal.py
```

The adapter runs only ArchMarshal bootstrap-status/doctor-style read checks, stores a binding fingerprint, and blocks dispatch/launch/recovery when a required binding drifts. Protected paths include `.agent`, `.agents`, `AGENTS.md`, `AGENTS.override.md`, `.git`, `.codex`, and `.codex-plugin`.

CostMarshal never automatically adopts a workspace, applies an ArchMarshal plan, starts/ends a managed session, or edits ArchMarshal itself. Perform those lifecycle operations explicitly through ArchMarshal, preview first, then initialize or rebind CostMarshal.

After upgrading CostMarshal's binding format, preview and explicitly apply a fresh
read-only fingerprint. This changes only CostMarshal project state and retains the
previous binding in bounded audit history; it never mutates ArchMarshal:

```powershell
python scripts/costmarshal.py governance-rebind --project <project-dir>
python scripts/costmarshal.py governance-rebind --project <project-dir> --apply --command-id CMD-GOVERNANCE-REBIND-001
```

## Recovery and validation

```powershell
python scripts/costmarshal.py recover --project <project-dir> --plan-restarts
python scripts/costmarshal.py recover --project <project-dir> --restart-missing
python scripts/costmarshal.py validate --project <project-dir>
python scripts/costmarshal.py status --project <project-dir> --format md
```

### Transactional control store

Existing projects remain on their legacy JSON/JSONL authority until an explicit, offline cutover. Preview is read-only; apply requires exclusive scheduler/project locks, rejects live actors, creates a complete backup, validates a staged database, installs it atomically, and writes the backend marker last:

```powershell
python scripts/costmarshal.py migrate-state --project <project-dir>
python scripts/costmarshal.py migrate-state --project <project-dir> --apply
python scripts/costmarshal.py state-store --project <project-dir>
python scripts/costmarshal.py state-store --project <project-dir> --repair-views
```

After cutover, scheduler mutations use SQLite WAL transactions and stable `--command-id` values. A reused ID with a different payload is rejected. JSON/JSONL files are compatibility views rebuilt after a commit-time crash. `dispatch --start`, escalation starts, and `stop-actor --stop-runtime` commit a fenced effect intent first; the scheduler leases the effect, performs OS/OCI I/O outside the database transaction, persists its observation, and atomically marks the effect and command complete. Expired leased or observed effects are recoverable. Launch tokens plus the attempt lifetime lock prevent a duplicate runner from calling the provider when a start observation is uncertain.

## Offline blind backtest

`scripts/backtest_shadow_matrix.py` evaluates an already collected real-provider
low/medium/high blind shadow matrix. It never reads credentials or calls a
provider. Without at least 200 attested real tasks it writes an honest blocked
`artifacts/backtest-report.json` and exits `2`. Dataset schema, checkpoint resume,
paired bootstrap confidence intervals, and budget rules are documented in
[`references/backtest.md`](references/backtest.md).

The non-beta release gate accepts no pre-existing evidence by itself. Invoke it
with `--reproduce-evidence`; that same process regenerates the complete local
test report, runtime crash report, offline backtest, and live OCI report before
evaluating their commit-bound contents. Without reproduction the gate remains
`blocked` even if hand-written artifact files claim success.

Release trust roots live in [`release/evidence-policy.json`](release/evidence-policy.json).
The beta ships them intentionally unset. A reviewed release must preregister
the blind policy manifest and signer allowlist in an earlier commit, and must
pin the worker repository digest plus provider-proxy image/configuration hashes.
Environment variables may supply evidence files and local object references;
they cannot replace the committed trust roots.

Real backtest reproduction also requires the source dataset, a detached
collection signature, and a trusted signer policy. Set
`COSTMARSHAL_BACKTEST_DATASET`, `COSTMARSHAL_BACKTEST_ALLOWED_SIGNERS`,
`COSTMARSHAL_BACKTEST_ATTESTATION_SIGNATURE`, and
`COSTMARSHAL_BACKTEST_SIGNER_IDENTITY` before invoking the release gate. Test
fixtures may exercise statistics with the harness's explicit unsigned-test
flag, but their report is permanently marked `test-only` and cannot satisfy a
release gate.

## Required local verification

```powershell
python -m compileall -q costmarshal_v2 tests
python tests/unit_test.py
python tests/smoke_test.py
python tests/local_backend_contract_test.py
python tests/tmux_contract_test.py
python tests/model_rotation_contract_test.py
python tests/three_tier_routing_test.py
python tests/route_oracle_test.py
python tests/backtest_harness_test.py
python tests/pricing_metadata_test.py
python tests/control_store_test.py
python tests/transactional_scheduler_test.py
python tests/runtime_effect_store_test.py
python tests/runtime_effect_scheduler_test.py
python tests/worker_isolation_test.py
python tests/worker_execution_adapter_test.py
python tests/container_worker_contract_test.py
python tests/oci_actor_runner_test.py
python tests/isolation_scheduler_gate_test.py
python tests/actor_crash_recovery_test.py
python tests/runtime_recovery_reliability_test.py
python tests/pid_identity_test.py
python tests/required_credential_preflight_test.py
python tests/actor_fencing_test.py
python tests/security_contract_test.py
python tests/actor_security_contract_test.py
python tests/reliability_contract_test.py
python tests/release_gate_test.py
python tests/budget_contract_test.py
python tests/budget_reconciliation_oracle_test.py
python tests/historical_state_migration_test.py
python tests/archmarshal_compat_test.py
python tests/profile_config_test.py
python tests/concurrency_contract_test.py
python scripts/install_smoke_test.py
python tests/release/run_local_test_evidence.py
python tests/release/run_runtime_effect_evidence.py
```

## Non-beta release evidence

These commands require the preregistered real-provider dataset/signature policy and
the reviewed live OCI/proxy topology. Without those external inputs, exit status 2
and a machine-readable `blocked` artifact are the expected safe result, not a local
test failure.

```powershell
python tests/oci_live_evidence.py
python tests/release/run_release_gates.py --reproduce-evidence
```

## Current hardening boundary

v2.4-beta has an opt-in SQLite WAL authority, marker-last migration with backups, dirty-view recovery, payload-hashed commands, leased spawn/stop effects, launch-token and lifetime-lock fencing, crash recovery after report publication, pricing snapshots, an independent 10,000-case route oracle, and an enabled required-mode OCI adapter plus reproducible worker-image source. Cached-input pricing is supported in immutable price snapshots, but task routing currently estimates cached tokens as zero; this is conservative and leaves cache-aware forecasting as a follow-up. It remains beta until the machine-readable release gates have real low/medium/high shadow-backtest evidence and live malicious OCI escape evidence for a reviewed image digest. A unit or mocked adapter test is not treated as that external proof.

## License

MIT
