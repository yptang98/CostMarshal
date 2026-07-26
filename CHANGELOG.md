# Changelog

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
