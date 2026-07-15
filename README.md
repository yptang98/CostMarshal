# CostMarshal

CostMarshal is a scheduler-first, cost-aware multi-provider control plane for Codex CLI. It coordinates low-, medium-, and high-tier APIs, keeps task and attempt state durable, and reserves the strongest provider for work that needs it.

Current version: `v2.3.0-beta`

> v2.3 adds real low/medium/high routing and product hardening. It does not claim that every deployment is economically optimal by default: reviewed prices, token estimates, and leader acceptance history are required before the cross-tier optimizer is enabled.

## What v2.3 does

- Routes bounded tasks across three capability tiers: low, medium, and high.
- Keeps provider identity separate from capability tier, so providers can be replaced without changing policy.
- Applies a fail-closed safety floor from risk, difficulty, and task type.
- Filters providers by explicit required capabilities before cost optimization.
- Compares priced execution chains such as `low → medium → high`, `medium → high`, and `high` by expected cost per leader-accepted result.
- Escalates one tier at a time and fences stale actor attempts.
- Uses durable actors, mailboxes, reports, claims, usage records, and recovery state.
- Reserves task/project budget at dispatch and settles it from usage or leader results.
- Requires an attested OCI boundary for production workers and never silently falls back to a native process.
- Provides an explicit SQLite WAL cutover for crash-atomic pure control-plane commands and recoverable compatibility views.
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

Clone or install this repository into the Codex skills directory, then restart Codex if its skill list is cached.

```powershell
git clone https://github.com/yptang98/CostMarshal.git "$env:CODEX_HOME\skills\costmarshal"
python "$env:CODEX_HOME\skills\costmarshal\scripts\install_smoke_test.py"
```

CostMarshal uses only Python's standard library at runtime. `git` is required for writable worker worktrees. `tmux` is optional; Windows defaults to the local process backend.

Production worker isolation additionally requires Docker or Podman running Linux containers and a digest-pinned CostMarshal worker image. Native workers are a development compatibility mode, not a security boundary.

## Provider setup

The default catalog contains:

| Provider ID | Tier | Default profile | Credential variable |
| --- | --- | --- | --- |
| `longcat` | low | `longcat` | `LONGCAT_API_KEY` |
| `deepseek` | medium | `deepseek` | `DEEPSEEK_API_KEY` |
| `codex` | high | current Codex profile | current Codex authentication |

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

Provide credentials through the process environment or a secrets file outside the actor workspace. CostMarshal injects only the selected low/medium provider key into that actor and creates a credential-free actor-specific `CODEX_HOME` containing only its profile.

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

The flat price fields in the compatibility example below remain readable for
existing v2.3-beta projects only. They have no provenance or freshness metadata,
cannot price cached input or fixed request fees, and do not produce an immutable
snapshot. New deployments should replace them with canonical nested `pricing`
objects.

```python
from costmarshal_v2.routing import build_pricing_snapshot

provider["pricing"] = build_pricing_snapshot(
    currency="CNY",
    source="https://vendor.example/reviewed-pricing",
    reviewed_at="2026-07-16T00:00:00Z",
    effective_at="2026-07-16T00:00:00Z",
    expires_at="2026-08-16T00:00:00Z",
    snapshot_id="vendor-2026-07",
    input_per_1m=2.0,
    cached_input_per_1m=0.5,
    output_per_1m=8.0,
    fixed_request=0.0,
)
```

```json
{
  "schema_version": 1,
  "providers": [
    {
      "provider_id": "low-api",
      "tier": "low",
      "profile": "low-api",
      "model": "model-low",
      "env_key": "LOW_API_KEY",
      "enabled": true,
      "priority": 100,
      "input_cny_per_1m": 0.8,
      "output_cny_per_1m": 2.0,
      "capabilities": ["text"]
    },
    {
      "provider_id": "medium-api",
      "tier": "medium",
      "profile": "medium-api",
      "model": "model-medium",
      "env_key": "MEDIUM_API_KEY",
      "enabled": true,
      "priority": 100,
      "input_cny_per_1m": 2.0,
      "output_cny_per_1m": 8.0,
      "capabilities": []
    },
    {
      "provider_id": "codex",
      "tier": "high",
      "profile": null,
      "model": "inherit",
      "env_key": null,
      "enabled": true,
      "priority": 100,
      "input_cny_per_1m": 12.0,
      "output_cny_per_1m": 48.0,
      "capabilities": []
    }
  ]
}
```

Unknown fields, duplicate IDs, invalid tiers, negative prices, malformed environment keys, and explicitly malformed catalogs are rejected.

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

Worker dispatch defaults to `worker_isolation.mode=required`. Docker/Podman selection may fail over only between supported OCI engines; it never falls back to a host process. Preflight rejects missing daemons, remote contexts, non-Linux engines, unpinned images, unsafe mounts, failed canaries, and unsupported network policy before an attempt or budget reservation is persisted.

The current beta contains the OCI contract and fail-closed preflight, but the final container execution adapter/image distribution is still gated. For trusted development tests only, both project initialization and each dispatch must opt in:

```powershell
python scripts/costmarshal.py init ... --allow-unsafe-native-workers
python scripts/costmarshal.py dispatch --project <project-dir> --task V2-0001 --unsafe-native --start
```

This records `strong_isolation=false`; it is forbidden when ArchMarshal governance is required. It does not protect host files from the model process.

Custom worker command templates are rejected by default because they bypass the controlled runner. `init --allow-unsafe-custom-worker-commands` exists only as an explicit compatibility escape hatch for trusted test/backends; it forfeits sandbox and secret-isolation guarantees.

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

After cutover, pure scheduler mutations use SQLite WAL transactions and stable `--command-id` values. A reused ID with a different payload is rejected. JSON/JSONL files are compatibility views rebuilt after a commit-time crash. Commands that spawn or stop an OS runtime are currently blocked after cutover until the recoverable effect worker is complete; this prevents a false exactly-once claim.

## Verification

```powershell
python -m compileall -q costmarshal_v2 tests
python tests/unit_test.py
python tests/smoke_test.py
python tests/local_backend_contract_test.py
python tests/tmux_contract_test.py
python tests/model_rotation_contract_test.py
python tests/three_tier_routing_test.py
python tests/pricing_metadata_test.py
python tests/control_store_test.py
python tests/transactional_scheduler_test.py
python tests/worker_isolation_test.py
python tests/isolation_scheduler_gate_test.py
python tests/actor_fencing_test.py
python tests/security_contract_test.py
python tests/actor_security_contract_test.py
python tests/reliability_contract_test.py
python tests/budget_contract_test.py
python tests/archmarshal_compat_test.py
python tests/profile_config_test.py
python tests/concurrency_contract_test.py
python scripts/install_smoke_test.py
```

## Current hardening boundary

v2.3-beta now has an opt-in SQLite WAL authority for pure commands, marker-last migration with backups, dirty-view recovery, command payload hashing, launch tokens, lifetime attempt locks, pricing freshness snapshots, and required-mode OCI preflight. It still remains beta because OS spawn/stop effects are not yet processed through the transactional effect outbox, and the attested OCI execution/image/report exchange path is not enabled. Required isolation therefore fails closed; only explicitly audited unsafe-native development dispatches can run today.

## License

MIT
