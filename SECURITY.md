# Security policy

## Reporting

Report suspected vulnerabilities through a private GitHub security advisory for
`yptang98/CostMarshal` when available. Do not include live API keys, secrets,
private prompts, or customer data in a public issue. Revoke an exposed provider
credential before collecting diagnostics.

## v3.0 trust boundaries

CostMarshal v3.0 is a prerelease. Its OCI controls isolate worker processes from
the host workspace, other provider credentials, mutable profiles, and scheduler
authority. They do not make the selected provider client hostile-safe.

The selected provider credential is mounted into the worker because Codex must
call that provider. Code and model-directed tools in the same container can read
it. Literal output redaction cannot prevent encoded, transformed, or split
exfiltration. Therefore:

- use a dedicated, least-privilege, spend-capped, rate-limited, revocable key;
- trust the reviewed digest-pinned worker image and workload not to steal it;
- never reuse a broad personal or organization-wide credential;
- do not use v3.0 for hostile workloads requiring credential confidentiality.

That hostile-workload threat model requires a separate credential broker or API
proxy that keeps the real key outside the worker and issues an
attempt/provider/budget/expiry-scoped capability. That broker is not included in
v3.0. The `provider-proxy` network validates controlled egress topology; it does
not by itself remove the raw credential from the worker.

Native worker mode is development compatibility only and is not a host security
boundary. ArchMarshal integration is read-only governance checking and does not
expand CostMarshal's authority.

## Certification

An empty `release/evidence-policy.json` means external real-provider economics
and malicious-OCI evidence have not been certified. Release gates must not turn
that absence into a production-certified claim.
