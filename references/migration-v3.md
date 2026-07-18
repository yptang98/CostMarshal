# CostMarshal v3.0 Migration

CostMarshal 3 keeps the existing `costmarshal_v2` Python package, state schema,
and `$CODEX_HOME/costmarshal-v2` runtime root so installed projects remain
discoverable. The product entry changes to the Codex plugin Skill
`orchestrate-cost-aware-agents`; a legacy `$costmarshal` Skill remains
explicit-only.

## Existing two-provider projects

An existing v2 project without a stored three-tier provider catalog remains a
readable two-tier project. CostMarshal does not silently edit its catalog or an
active route envelope. To adopt low/medium/high routing, create a new v3 project
and initialize it with the reviewed three-tier catalog (or the v3 default
catalog), then create new tasks there. Keep the old project unchanged for audit
and recovery; do not hand-edit `project.json` or copy active task envelopes.

## Updating a pinned Codex plugin

`codex plugin marketplace upgrade` refreshes the ref already recorded for a Git
source; it does not change an old exact commit to a new exact commit. Replace a
pinned snapshot with `plugin remove`, `marketplace remove costmarshal`,
`marketplace add yptang98/CostMarshal --ref <new-sha>`, then `plugin add`. Both
`$CODEX_HOME/costmarshal-v2` and legacy `$CODEX_HOME/costmarshal` runtime roots
must remain byte-for-byte untouched throughout the replacement.

## Routing compatibility

- New projects store `routing_policy.version = 3` and default to
  `routing_objective = completion-first`.
- Existing projects without `routing_objective` remain `cost-only`; they are
  never silently given a stronger or more expensive terminal plan.
- A task freezes its effective objective and source. Changing the project
  default does not rewrite existing tasks or sealed routes.
- Route-plan v1 fingerprints remain byte-compatible for old steps. New steps
  use route-plan v2 and bind the routing objective plus each step's token/cache
  forecast.

## Budget and cache compatibility

- New admissions use route-budget-envelope v3 and collaboration-contract v2.
- Released v2 envelopes remain auditable. Active v2 single-step envelopes and
  active multi-step envelopes with zero cached input remain executable.
- An active v2 multi-provider envelope with cached input cannot prove cache
  portability and fails closed. Create a new task; do not rewrite the sealed
  fingerprint or price history.
- New first steps reclassify cached input as ordinary when no complete origin is
  proven; successor steps also reclassify whenever provider, model, profile, or
  profile SHA-256 differs.
- An explicit all-zero final usage observation may settle a reviewed
  `fixed_attempt`. A missing usage observation and historical ambiguous zero row
  remain unsettled.

## Runtime compatibility

- SQLite cutover remains explicit and marker-last. Existing JSON-authority
  projects are not migrated automatically.
- Explicit recovery increments a fenced generation; prior runner registration
  cannot authorize the new generation.
- Windows local actors now require an exact Job Object receipt. Legacy PID-only
  local state cannot be assumed live or safely restarted and therefore fails
  closed.
- ArchMarshal integration remains read-only. CostMarshal never adopts, applies,
  or mutates an ArchMarshal workspace during migration.
