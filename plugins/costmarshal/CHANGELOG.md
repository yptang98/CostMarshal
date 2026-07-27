# Changelog

## v4.3.3 - 2026-07-28

- Added an expiring, committed production-build input review that pins the
  official Python and Node OCI indexes, their exact linux/amd64 manifests, and
  the observed runtime versions.
- Replaced mutable global Codex installation with a committed npm lockfile,
  verified top-level integrity, platform-binary integrity, and
  `npm ci --ignore-scripts`.
- Added a Linux CI packaging gate that builds the exact production Gateway and
  Worker Dockerfiles, checks immutable revision labels and non-root users,
  validates a policy inside the Gateway, and executes the Worker isolation
  canary with read-only rootfs, dropped capabilities, no network, and bounded
  mounts.
- Restricted the manual production-image workflow to its exact checked-out
  source SHA and committed build inputs. It independently verifies the selected
  base manifests and records the build-input and lockfile identities alongside
  the published image digests, provenance, and SBOMs.

## v4.3.2 - 2026-07-28

- Fixed the single-host production Compose network contract by labelling the
  internal `costmarshal-provider-proxy` bridge exactly as the OCI Worker
  isolation layer requires.
- Published the Proxy port on configurable loopback only so the scheduler and
  evidence collectors can verify it without granting external ingress.
- Strengthened deployment preflight with parseable CA bundles, matching TLS
  certificate/private-key pairs, mTLS client material, and credential-free
  HTTPS endpoint validation.
- Strengthened deployment completion with immutable Docker network
  attestation and policy-SHA-bound TLS health probes for both Broker and
  Proxy. TCP-only readiness can no longer produce a successful receipt.
- Added a secret-free production environment template covering every required
  image, endpoint, state, credential, and TLS path.

## v4.3.1 - 2026-07-27

- Added `v4` to the cross-platform CI branch contract.
- Added a manual, least-privilege GHCR production-image workflow. It validates
  digest-pinned base images and the exact release ref, reruns the complete
  local evidence suite, publishes commit-only Worker/Gateway images with SBOM
  and BuildKit provenance, and emits an immutable digest receipt.
- Added a root allowlist `.dockerignore` so gateway builds never transmit Git
  metadata, generated evidence, plugins, tests, or unrelated repository files
  to the builder.
- Added Broker and Proxy readiness checks and made deployment success require
  both Compose services to report `running` and `healthy`.
- Restricted the production gateway SQLite path to a direct filename under
  the Compose-mounted `/var/lib/costmarshal` state directory.
- Corrected cross-platform runtime-evidence aggregation: Linux-only OCI
  recovery evidence now reports `blocked` on non-Linux hosts instead of
  producing a false release failure, while the gate still requires Linux
  evidence before certification.
- Documented and contract-tested LongCat-2.0 as text-only after live semantic
  probes showed that both Responses and Chat accepted image-shaped requests
  with HTTP 200 but did not expose either data-URI or public-URL images to the
  model. Transport success can no longer be mistaken for visual capability.
- Hardened the local release-evidence runner with isolated process groups and
  bounded whole-tree termination. A timed-out test can no longer leave a
  descendant holding the captured output pipe and stall the release suite
  indefinitely.

## v4.3.0 - 2026-07-27

- Added immutable audio, video, and document attachment receipts. Every input
  is bound to an exact committed Git blob, collaboration contract, routed
  capability, projected file hash, Actor execution mode, and OCI command.
- Added a report-only `multimodal-api` worker path for gateway-bound native
  Responses providers. It supports bounded image/audio/video/document payloads,
  authoritative usage, hard Proxy settlement, and no workspace writes or
  tool execution; normal Codex Agent mode remains image-only.
- Added Provider observation and review lifecycles. API schema, pricing,
  capability, or behavior drift can only block or lower route confidence;
  restoring or expanding a provider requires an expiring human-reviewed
  catalog row bound to immutable observations.
- Replaced the single LongCat Compose secret with a Proxy-only provider
  credential directory so one deployment can serve reviewed DeepSeek, Kimi,
  LongCat, MiMo, and Doubao policies without mounting keys into the Broker.
- Added a preview-first production deployment entrypoint that validates the
  clean release commit, gateway policy, digest-pinned image, Docker Compose,
  bounded secret files, directory ownership, and exact running services before
  writing a secret-free deployment receipt.

## v4.2.0 - 2026-07-27

- Split live gateway readiness from external production certification. Healthy
  Broker/Proxy probes and accepted Artifact IDs can no longer certify a
  deployment on their own.
- Added canonical, at-most-seven-day production claims verified with an
  OpenSSH detached signature and a hash-pinned `allowed_signers` trust root.
- Bound certification to the exact source commit, release version, production
  boundary, gateway policy, Worker and gateway image digests, and report
  SHA-256 receipts for all five required external evidence types.
- Added `production-evidence` project Artifacts and a create/verify helper that
  never reads or stores the external signing private key.
- Enforced production dispatch now requires both live gateway readiness and a
  currently valid signed certification; legacy v1/v2 boundaries remain
  readable but cannot claim external certification.
- Added bounded OpenAI Responses-to-Chat Completions translation for text,
  image, audio, function tools, structured output, completed JSON, and buffered
  SSE. Native Responses remains the required path for video and document input.
- DeepSeek and Kimi presets can now emit executable gateway-bound catalog rows
  with `--via-production-gateway`; direct execution remains rejected, and
  dispatch requires a ready enforced production boundary.
- Added a live, secret-free Provider schema-drift probe that binds a real
  Responses canary and normalized response-shape fingerprint to the exact
  commit and gateway policy.

## v4.1.0 - 2026-07-27

- Added a deployable mTLS Credential Broker with exact SPIFFE workload
  allowlists and signed provider/model/token/budget/expiry-scoped leases.
- Added a provider-key-isolating Proxy with SQLite-atomic nano-CNY reservation,
  request idempotency, conservative settlement, and overrun lease revocation.
- Added live TLS health probes bound to the reviewed gateway-policy SHA-256;
  enforced dispatch fails closed when either service is unavailable or drifts.
- Required OCI Actors can rewrite a verified provider profile to the Proxy,
  mount only the short-lived lease, and trust a read-only private CA bundle.
- Added hardened single-host Compose/container deployment templates and
  production gateway/Actor/deployment contract tests.

## v4.0.0 - 2026-07-26

### Added

- Added immutable Git repository identities and per-task repository ownership
  without moving, adopting, or modifying source repositories.
- Added first-class Workstreams with repository membership, dependency Gates,
  concurrency quotas, and bounded CNY allocations.
- Added staged per-repository integration plans bound to the complete current
  Workstream task set, accepted interface Artifacts, exact Git heads, and
  verified rollback ancestors.
- Added Leader-owned integration Gates and status/validation surfaces for
  repository, Workstream, plan, and Gate state.
- Added a secret-free production-boundary contract for external Credential
  Broker and Provider Proxy evidence.

### Safety

- Cross-repository integration is explicitly non-atomic and never mutates
  source repositories while planning or evaluating a Gate.
- A passed Gate closes its Workstream to new tasks; downstream dispatch trusts
  only hash-valid Gate/plan pairs.
- The external Broker runtime adapter remains unimplemented. Production status
  is honestly `blocked`, and enforced mode fails dispatch closed rather than
  claiming deployment certification from local configuration.

## v3.5.0 - 2026-07-26

### Added

- Added evidence-bound total-cost reports centered on cost per accepted
  Artifact, with execution, verification, rework, context/handoff, Leader
  attention, and failure/recovery dimensions.
- Added structured teaching execution graphs and validated teaching-run
  ledgers for independent review, paired comparison, and fixed replay.
- Added 95% Wilson intervals, a 90-day evidence half-life, effective sample
  counts, and recency-weighted scores to exact model/profile/task memory.

### Changed

- Exact provider/model/profile/profile-hash identity continues to isolate model
  versions; stale observations now reduce aggregate confidence.
- Routing retains externally caused failures for audit but excludes routing,
  context, tool, environment, dependency, budget, and human-review failures
  from negative model-capability evidence.
- New structured-teaching tasks require a validated teaching run for enforced
  acceptance; legacy free-form evidence remains read-compatible only.

### Safety

- Total-cost reports never invent monetary prices for observed time, tokens,
  handoffs, or unknown provider/Leader costs. The metric is marked partial or
  unavailable when evidence is incomplete.
- Teaching evidence binds exact result and Gate hashes and cannot activate a
  learned policy; replay, shadow, canary, and explicit activation remain
  separate reviewed transitions.

## v3.4.0 - 2026-07-26

### Added

- Added accepted-evidence-only project knowledge for Charter, Architecture,
  ADR, Interface, Accepted Fact, Risk, and Milestone Summary records.
- Added immutable explicit Leader decisions as an alternative auditable source
  for project knowledge.
- Added deterministic project/milestone summary manifests with accepted
  Artifact lineage and logical `YYYY/MM/DD_name` views.
- Added project-local Skill Candidate metadata requiring successful evidence
  from at least two distinct tasks.
- Added explicit Skill export preview/apply inside the project runtime; export
  never writes to or installs a global Skill directory.

## v3.3.0 - 2026-07-26

### Added

- Added deterministic, transcript-free `leader-snapshot-v1` records bound to
  Work Graph, budget, and Artifact revisions.
- Added `structured-handoff-v2` with conclusion, facts, typed evidence,
  unresolved issues, and next actions; new projects write only this schema
  while legacy text capsules remain readable.
- Added SQLite-atomic batch acceptance with independent per-task Gates and
  complete rollback when any decision fails.
- Added current-project Artifact registration, immutable lineage, logical date
  buckets, external-only large-artifact references, and filtered queries.

### Safety

- Artifact registration never copies, moves, overwrites, or deletes sources.
- Local references are confined to the current runtime/workspace/read-only
  source project and are reverified by `validate`; external URIs reject
  credentials, query strings, and fragments.
- Skill candidates remain project-local metadata and are never installed into
  a global Skill directory.

## v3.2.0 - 2026-07-26

### Added

- Added reviewed provider/model presets for DeepSeek V4, Kimi K3/K2.6,
  LongCat 2.0, Xiaomi MiMo 2.5, and Doubao Seed 2.0 Lite.
- Added a machine-readable `provider-presets` diagnostic and
  `configure-provider --preset`, returning a safe profile plus an unpriced
  provider-catalog template.
- Separated documented API capabilities from effective end-to-end worker
  capabilities so unsupported modalities cannot enter hard routing.
- Added committed local image inputs through `new-task --input-image`, immutable
  context projection, native Codex execution, and the OCI worker bootstrap.

### Changed

- Populated the default provider catalog with conservative text/agent
  capabilities while preserving existing model/profile execution identities.
- Kept LongCat on the Responses wire protocol required by current Codex after a
  live deployment check, while recording that its public docs describe Chat
  Completions.
- Documented the canonical capability vocabulary and the current audio/video
  transport limitation; provider pricing remains separately reviewed.

## v3.1.1 - 2026-07-26

### Fixed

- Replaced the non-standard `CODEX_API_KEY` contract with Codex CLI's standard
  `OPENAI_API_KEY` for isolated OpenAI execution.
- Clarified that native Codex execution reuses the existing Codex sign-in,
  while required OCI workers use an explicitly scoped API key because they
  cannot inherit the host session.
- Added a narrow read-time normalization for v3.1.0's default Codex catalog
  entry without changing custom provider credential contracts.
- Made Windows CI verify bounded retry counts and durable effect recovery
  instead of treating shared-runner scheduling pauses as product failures.

## v3.1.0 - 2026-07-25

### Added

- Durable Work Graph nodes with roles, dependency joins, readiness states, and
  a dispatch gate that requires accepted predecessors.
- Content-addressed artifact registry and deterministic dependency, leader,
  quality, error-severity, deliverable, and teaching gates.
- Immutable six-dimensional attempt evaluations, explicit failure attribution,
  automatic project retrospectives, and non-activating policy candidates.
- Rebuildable cross-project model capability/quality memory and a teaching
  policy for cold-start, high-risk, low-confidence, and repeated-failure scopes.
- `work-graph` and `model-memory` diagnostics plus task/result CLI fields for
  dependencies, deliverables, roles, gates, teaching, scores, and artifacts.

### Routing and safety

- Safe automatic routing can use audited evidence from sibling projects under
  the same runtime root.
- New evidence distinguishes leader acceptance from quality-aware routing
  success; low-quality or high-error accepted work does not train the economic
  router as a success.
- Learned policy remains staged (`candidate`, replay, shadow, canary, active)
  and never self-activates from a single observation.

## v3.0.0 - 2026-07-16

CostMarshal is now a Codex-native plugin with one implicit orchestration Skill;
the Python scheduler remains an internal, compatibility-preserving runtime.

### Added

- Low, medium, and high provider tiers with bounded same-tier peers and
  non-decreasing escalation chains.
- Completion-first routing by default for new projects, with an explicit
  cost-only opt-out and objective-bound route fingerprints.
- Per-step token forecasts, conservative cache identity handling, immutable
  price/profile evidence, fixed-attempt fees, and v3 budget envelopes.
- Recoverable SQLite runtime effects, generation fencing, Windows Job Object
  supervision, exact process identity, and durable provider completion.
- Codex plugin manifest, a curated `plugins/costmarshal` marketplace snapshot,
  remote exact-SHA install/update smoke, and migration guidance for existing v2
  state and two-provider projects.

### Security and correctness

- Windows npm `codex.cmd` is resolved to a native Node argv; arbitrary batch
  actor commands are rejected before launch.
- Missing provider usage stays unknown and unsettled; only an explicit token
  observation can prove an all-zero usage receipt.
- Cached input without a complete frozen origin is priced as ordinary input.
- Native high-tier `auth.json` copying now requires stable non-link bytes and an
  immutable actor-private file with restrictive permissions.

### Compatibility

- The internal `costmarshal_v2` package, v2 task IDs, and
  `$CODEX_HOME/costmarshal-v2` runtime root are intentionally retained.
- Old route-plan v1 fingerprints remain auditable. Existing projects without a
  routing objective retain cost-only behavior.
- ArchMarshal remains a read-only governance dependency; CostMarshal never
  adopts or mutates an ArchMarshal workspace.

### Prerelease certification boundary

The v3.0.0 GitHub release is a functional prerelease, not a
production-certified claim. External real-provider shadow evidence and live
malicious-OCI evidence are not pinned in the source-checkout-only
`release/evidence-policy.json`.
Additionally, the selected provider credential shares the worker/provider-client
trust domain; see `SECURITY.md`.
