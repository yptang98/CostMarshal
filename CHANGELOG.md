# Changelog

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
- Codex plugin manifest, personal marketplace entry, install/update smoke, and
  migration guidance for existing v2 state and two-provider projects.

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
malicious-OCI evidence are not pinned in `release/evidence-policy.json`.
Additionally, the selected provider credential shares the worker/provider-client
trust domain; see `SECURITY.md`.
