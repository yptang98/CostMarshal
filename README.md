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
- Isolates low/medium provider credentials and runs writable worker tasks in detached git worktrees.
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

Create a JSON file and pass it to `init --provider-catalog`. Prices are CNY per one million tokens; use `null` until a human has reviewed the value.

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
  --governance off
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

Writable low/medium tasks require a clean git repository. They execute in a detached worktree under the CostMarshal runtime. The runner rejects diffs outside `allowed_paths`/`claimed_paths`; the leader reviews and integrates the isolated changes. A task with no write paths sees the source workspace read-only.

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

## Verification

```powershell
python -m compileall -q costmarshal_v2 tests
python tests/unit_test.py
python tests/smoke_test.py
python tests/local_backend_contract_test.py
python tests/tmux_contract_test.py
python tests/model_rotation_contract_test.py
python tests/three_tier_routing_test.py
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

v2.3 has OS-backed single-writer locking, a scheduler singleton lock, attempt fencing, command replay guards, worktree isolation, secret minimization, and budget reservations. The remaining path to a non-beta release is a transactional state backend for crash-atomic multi-file dispatch/escalation plus a stronger OS/container read boundary for provider workers. Until then, use `validate` and `recover` after an unclean process or machine termination.

## License

MIT
