# Release evidence policy

`evidence-policy.json` is the reviewed trust root for production-certification
gates. The v3.0 prerelease keeps every value unset, so tests can exercise the machinery without allowing a
caller-provided key or image to become release evidence.

Backtest preregistration is two-step:

1. Before collection/review, commit the approved `allowed_signers` SHA-256,
   signer identities, and frozen CostMarshal policy-manifest SHA-256 while
   leaving `preregistered_commit` unset.
2. In a later commit, set `preregistered_commit` to that first commit. The gate
   verifies it is a strict ancestor and that its trust fields are byte-for-byte
   unchanged.

Before live OCI release evidence, also pin the worker repository digest, the
provider proxy's immutable image/configuration hashes, and the hashes of its
credential-free health URL and expected allowlisted response. The live harness
prints those hashes only after independently inspecting a running, labelled,
dual-homed proxy, its non-internal egress network, and a hash-bound allowlisted
response reached from the worker through that proxy. Any policy update is a normal reviewed Git change; environment
variables can locate local artifacts but cannot override these values.

Release evidence and deployment certification are related but distinct.
`evidence-policy.json` governs the source release gate. A deployed v4.3 project
also stores a v3 production boundary with its own reviewed `allowed_signers`
SHA-256, signer identities, exact commit/release, and gateway image digest. The
external reviewer signs the canonical, short-lived
`costmarshal-production-certification-v1` claim only after all five report
hashes are registered as accepted `production-evidence` Artifacts. The private
signing key is never stored in CostMarshal.

The external evidence set also includes
`tests/release/run_provider_drift_evidence.py`. It performs one real Responses
canary through the hash-bound gateway and records only the response hash and
normalized schema shape. A missing usage/output contract or a changed field
type fails the drift evidence instead of silently lowering routing confidence.
