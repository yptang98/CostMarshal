# Changelog

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
