# CostMarshal v4.5 Large-project Coordination

CostMarshal's project layer exists to make one current project easier to
execute, review, resume, and reuse. It is not a global project manager, does
not reorganize the user's workspace, and never installs or manages global
Skills.

## Repository identities

`register-repository` records a canonical Git root, role, committed HEAD, and
content hash. Registration is metadata-only and sets `source_mutation=false`.
The identity cannot be rebound to a different path or role. Current HEAD drift
is observable, while a task remains bound to the immutable registered
identity.

The repository chosen by `new-task --repository` becomes that task's execution
workspace. Explicit multi-repository write claims are namespaced by repository
inside the central lock table, so equal relative paths in different
repositories do not conflict. Existing default-repository projects keep their
legacy lock paths.

## Workstreams

A Workstream is a project-local coordination boundary with:

- one or more registered repositories;
- predecessor Workstreams;
- a positive concurrency limit;
- an optional CNY allocation bounded by the project budget; and
- the tasks explicitly created inside it.

A task cannot dispatch while a predecessor lacks a valid passed integration
Gate, while the Workstream concurrency quota is full, or when projected task
commitment exceeds the Workstream allocation. A passed Gate closes the
Workstream to new tasks.

## Staged integration

`create-integration-plan` is a read-only freeze, not a commit operation. It
requires:

- every current non-cancelled task in each selected Workstream;
- every repository used by those tasks;
- accepted interface Artifact IDs, when supplied;
- each repository's exact current HEAD; and
- an exact rollback commit that exists and is an ancestor of that HEAD.

The strategy is always `staged-per-repository`,
`atomic_cross_repository=false`, and `source_mutation=false`.

`integration-gate` rechecks the complete Workstream task set, Leader acceptance
for every task, current acceptance of interface Artifacts, and unchanged Git
heads. It records every check and the explicit Leader approver. Only a
hash-valid passed Gate may unlock dependent Workstreams.

CostMarshal does not perform a cross-repository two-phase commit, push branches,
or roll back source repositories automatically. Those remain explicit,
separately reviewed integration actions.

## Production boundary

`configure-production-boundary` stores a secret-free external contract:

- HTTPS Credential Broker endpoint and workload identity;
- HTTPS Provider Proxy endpoint and hard-budget posture; and
- accepted Artifact IDs for real-provider backtest, live OCI adversarial,
  Broker attestation, Proxy budget attestation, and schema-drift monitoring.

URLs with credentials, query strings, fragments, or non-HTTPS schemes are
rejected. No provider secret is stored in the document.

With `--runtime-adapter costmarshal-gateway-v1`, the contract activates the
deployable gateway runtime:

- the Broker requires a client certificate with exactly one allowlisted SPIFFE
  URI SAN and issues a signed attempt-scoped lease;
- the lease binds provider, model, token envelope, integer nano-CNY budget,
  expiry, and reviewed gateway-policy hash;
- the Proxy is the only service that mounts the real provider key;
- SQLite reserves budget atomically before an upstream call, request IDs cannot
  repeat a call, unknown usage consumes the reservation, and overrun disables
  the lease;
- the OCI Actor rewrites its verified profile to the Proxy URL and mounts only
  the lease plus a read-only CA bundle; and
- dispatch probes both live TLS services and checks the exact policy hash.

The v4.2 boundary additionally pins:

- the exact 40-hex deployed commit and release version;
- the digest-pinned gateway image;
- the SHA-256 of an external OpenSSH `allowed_signers` file; and
- one or more trusted signer identities.

`production-status` reports `runtime_status` and `certification_status`
separately. The final status remains `blocked` unless both are ready. A valid
certification is an at-most-seven-day canonical manifest signed in the
`costmarshal-production-certification-v1` namespace. It binds the exact
boundary/policy/commit/release/image values and the report SHA-256 in each
accepted `production-evidence` Artifact receipt. Legacy raw-key mode and legacy
v1/v2 production boundaries cannot satisfy an enforced production boundary.

The included Compose topology is a hardened single-host deployment. It is not
multi-host HA and its SQLite ledger must not be placed on NFS. See
[`deploy/production/README.md`](../deploy/production/README.md).

## Minimal command sequence

```powershell
python scripts/costmarshal.py register-repository --project <project> --repository-id api --path <git-root> --role service --command-id CMD-REPO-001
python scripts/costmarshal.py create-workstream --project <project> --workstream-id foundation --name "Foundation" --objective "<objective>" --repository api --budget-cny 10 --concurrency-limit 2 --command-id CMD-WS-001
python scripts/costmarshal.py new-task --project <project> --repository api --workstream foundation --title "<title>" --purpose "<purpose>"
python scripts/costmarshal.py workstreams --project <project>
```

Use `repositories`, `workstreams`, `status`, and `validate` as the read-only
inspection surfaces. See `--help` for the full integration and production
boundary arguments.
