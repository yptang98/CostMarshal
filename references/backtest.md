# Blind Shadow-Matrix Backtest

`scripts/backtest_shadow_matrix.py` is an offline release-evidence evaluator. It
does not call providers, access the network, or read API credentials. Real
provider executions and blind reviews must be collected separately under an
approved protocol.

## Honest blocked behavior

Running without an attested dataset writes `artifacts/backtest-report.json`
with `status: blocked` and exits `2`:

```powershell
python scripts/backtest_shadow_matrix.py
```

The harness never creates synthetic release evidence. Synthetic, unblinded,
under-sized, incomplete, or unattested matrices are also blocked.

Self-asserted JSON booleans are not an attestation. Even a structurally valid
matrix is blocked from release scope unless its exact raw bytes have a valid
detached signature from an explicitly trusted signer.

## External detached attestation

Release evaluation uses the system `ssh-keygen -Y verify` implementation with
the fixed namespace `costmarshal-backtest-v2`. The caller must explicitly supply:

- an OpenSSH `allowed_signers` file maintained outside the dataset;
- the detached signature over the exact dataset bytes;
- the signer identity expected in the trusted file.

For example, an authorized collection/review service signs the frozen dataset:

```powershell
ssh-keygen -Y sign `
  -f C:\secure\attestation-ed25519 `
  -n costmarshal-backtest-v2 `
  C:\secure\real-shadow-matrix.json
```

The trusted file contains an identity and public key, for example:

```text
costmarshal-release@example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA...
```

Evaluation then verifies the signature before parsing or trusting the dataset:

```powershell
python scripts/backtest_shadow_matrix.py `
  --dataset C:\secure\real-shadow-matrix.json `
  --allowed-signers C:\secure\allowed_signers `
  --attestation-signature C:\secure\real-shadow-matrix.json.sig `
  --signer-identity costmarshal-release@example.test
```

Missing `ssh-keygen`, missing files, an untrusted identity, a namespace mismatch,
or any byte-level dataset change produces `status: blocked`,
`evidence_scope: blocked`, and `external_attestation_verified: false`.

For the non-beta release gate, CLI-supplied files are evidence inputs rather
than trust roots. The allowlist byte hash, approved signer identity, and policy
manifest hash must match `release/evidence-policy.json` as committed in a
strict ancestor of the evaluated release commit. This preregistration prevents
a result-aware policy or a caller-generated self-signing key from being
promoted to release evidence.

Unit tests may explicitly use `--allow-unsigned-test-fixture` to exercise only
schema and statistics. Such a run may have `status: pass`, but it always reports
`evidence_scope: test-only`, `external_attestation_verified: false`,
`real_provider_shadow_matrix: false`, and
`credentialed_collection_completed: false`. It is never release evidence and
cannot be combined with signature options.

## Dataset contract

Release evidence requires at least 200 unique tasks and coverage of low,
medium, and high safety floors. Dataset schema v2 requires every task to contain
one separately blind-reviewed outcome for every enabled provider in the frozen
policy catalog. Multiple providers may share a tier. Reviewer-visible records
must not expose provider, model, or tier identity. The signed, post-review
dataset keys outcomes by provider ID only after unblinding and separately maps
each blind result ID to that frozen provider ID and its provider-call hash.

Provider call identifiers are stored only as SHA-256 hashes. Blind result IDs,
provider-call hashes, task input hashes, and provider mappings must be complete
and unique. A missing provider outcome, duplicate mapping, or swap between two
provider outcomes and their unblinding entries is blocked. The outer dataset
signature protects the exact mapping bytes; the preregistered policy-manifest
hash protects the provider catalog and route policy that existed before review.
Legacy schema v1 tier-keyed matrices are explicitly blocked rather than guessed
or silently upgraded. They must be recollected/exported under schema v2 and
signed with the v2 attestation namespace.

Top-level fields:

```json
{
  "schema_version": 2,
  "study_id": "reviewed-study-id",
  "real_provider_shadow_matrix": true,
  "synthetic": false,
  "collection_attestation": {
    "real_provider_calls_completed": true,
    "credentialed_collection_completed": true,
    "provider_call_ids_hashed": true,
    "blind_review_records_frozen_before_unblinding": true,
    "unblinding_maps_frozen_catalog_provider_ids": true
  },
  "blinding": {
    "reviewer_blinded_to_provider": true,
    "reviewer_blinded_to_tier": true,
    "outcomes_unblinded_after_policy_lock": true
  },
  "policy_manifest_sha256": "sha256:<64 lowercase hex>",
  "policy_manifest": {},
  "tasks": []
}
```

The empty outcome objects above abbreviate the same exact five-field outcome
shape shown for `reviewed-low-a`; empty or partial outcomes are invalid in an
actual dataset, and every blind ID and provider-call hash must be unique.

## Hash-bound policy manifest

Candidate and baseline chains are not trusted merely because they appear in a
task row. The dataset must contain a pre-review policy manifest with the exact
CostMarshal commit, provider catalog, acceptance history, deterministic routing
time, per-task routing inputs, candidate/baseline requests, and the provider IDs
that were locked before review. The following is an abridged shape;
`provider_catalog` must be the full validated catalog (the empty provider list
is not itself runnable evidence):

Every task route uses exactly the manifest `locked_at` time, and the bound
history must not contain outcomes from any task in the study. This prevents a
post-review route recomputation from being re-hashed as if it were pre-locked.

```json
{
  "schema_version": 1,
  "routing_engine": "costmarshal_v2.routing.decide_route",
  "git_sha": "<40-character checked-out commit>",
  "locked_at": "2026-07-01T00:00:00Z",
  "provider_catalog": {"schema_version": 1, "providers": []},
  "history": [],
  "task_routes": {
    "BT-0001": {
      "task": {
        "risk": "low",
        "difficulty": "normal",
        "task_type": "analysis",
        "required_capabilities": []
      },
      "input_tokens": 100000,
      "output_tokens": 12000,
      "now": "2026-07-01T00:00:00Z",
      "candidate_request": {
        "requested_provider_id": null,
        "requested_tier": null
      },
      "baseline_request": {
        "requested_provider_id": "reviewed-high-provider",
        "requested_tier": null
      },
      "candidate_provider_ids": ["reviewed-low-provider", "reviewed-medium-provider", "reviewed-high-provider"],
      "baseline_provider_ids": ["reviewed-high-provider"]
    }
  },
  "manifest_sha256": "sha256:<64 lowercase hex>"
}
```

`manifest_sha256` is SHA-256 over canonical UTF-8 JSON of the manifest with the
`manifest_sha256` field removed (`sort_keys=true`, separators `,` and `:`, no
NaN). The top-level `policy_manifest_sha256` must repeat it. The evaluator
requires the manifest commit to equal the checked-out commit, reruns
`costmarshal_v2.routing.decide_route` for both policies, verifies the recorded
provider-ID chains directly, maps them to tiers through the bound catalog to
enforce monotonicity and safety floors, and rejects any task chain or safety
floor that differs. The manifest `locked_at` must equal every
task's `policy_locked_at` and precede blind-review completion.

Each task records the resulting pre-unblinding candidate and baseline provider
chains. Their tiers must be strictly increasing, cannot start below the safety
floor, and the provider IDs must exactly match the recomputed hash-bound policy
manifest. Outcomes are provider-ID keyed only in the signed post-unblinding
dataset; the separate map proves which blind record and provider-call hash were
assigned to each frozen provider:

```json
{
  "task_id": "BT-0001",
  "task_input_sha256": "sha256:<64 lowercase hex>",
  "safety_floor": "low",
  "candidate_provider_ids": ["reviewed-low-a", "reviewed-medium", "reviewed-high"],
  "baseline_provider_ids": ["reviewed-high"],
  "task_budget_cny": 2.0,
  "policy_locked_at": "2026-07-01T00:00:00Z",
  "review_completed_at": "2026-07-02T00:00:00Z",
  "outcomes": {
    "reviewed-low-a": {
      "blind_result_id": "opaque-result-001",
      "provider_call_id_hash": "sha256:<64 lowercase hex>",
      "accepted": true,
      "quality_score": 5,
      "actual_cost_cny": 0.1
    },
    "reviewed-low-b": {},
    "reviewed-medium": {},
    "reviewed-high": {}
  },
  "unblinding": {
    "opaque-result-001": {
      "provider_id": "reviewed-low-a",
      "provider_call_id_hash": "sha256:<same 64 lowercase hex>"
    }
  }
}
```

## Run and resume

```powershell
python scripts/backtest_shadow_matrix.py `
  --dataset C:\secure\real-shadow-matrix.json `
  --allowed-signers C:\secure\allowed_signers `
  --attestation-signature C:\secure\real-shadow-matrix.json.sig `
  --signer-identity costmarshal-release@example.test `
  --checkpoint artifacts\backtest-checkpoint.json `
  --output artifacts\backtest-report.json `
  --bootstrap-samples 5000 `
  --project-budget-cny 100
```

The checkpoint is an untrusted cache. It is bound to the dataset hash, policy
manifest hash, exact CostMarshal git SHA, minimum coverage, bootstrap sample
count and seed, cost/acceptance thresholds, and project budget through a
canonical configuration hash. On resume, every cached aggregate row is
recomputed from the dataset and compared byte-canonically before it is reused.
The configuration also binds release/test scope, signer identity, trusted signer
file hash, and signature hash, so an unsigned checkpoint cannot be promoted by a
later signed invocation.
After interruption, rerun with the same semantic options. A row modification,
configuration change, corrupt checkpoint, policy change, code change, or
dataset hash change fails closed.

`--max-tasks N` processes at most N additional tasks and is useful for bounded
collection/recovery drills. An incomplete checkpoint produces `status: blocked`
and exit `2`.

## Metrics and release thresholds

Candidate and baseline chains stop at the first accepted blind outcome. The
harness computes:

- paired acceptance-rate difference;
- candidate and baseline CNY per accepted task;
- paired bootstrap 95% confidence intervals;
- per-task and optional project budget overruns.

Defaults require the cost-ratio CI upper bound to be below `1.0`, acceptance
delta CI lower bound to be at least `-0.02`, and zero budget overruns. Completed
evidence also requires at least 95% of paired bootstrap samples to yield a
defined cost-per-accepted ratio, preventing undefined samples from being
silently discarded. Evidence that misses a threshold has `status: fail` and
exits `1`. Only valid, complete evidence meeting every threshold has
`status: pass` and exits `0`.

The report includes the dataset and policy-manifest SHA-256 values, exact
evaluation configuration and its SHA-256, fixed bootstrap seed, floor counts,
checkpoint status, thresholds, point metrics, confidence intervals, and
the external-attestation scope, signer and trust-file/signature hashes, plus
explicit confirmation that the harness used no credentials and made zero
provider calls.
