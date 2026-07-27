# Security policy

## Reporting

Report suspected vulnerabilities through a private GitHub security advisory for
`yptang98/CostMarshal` when available. Do not include live API keys, secrets,
private prompts, or customer data in a public issue. Revoke an exposed provider
credential before collecting diagnostics.

## v4.1 trust boundaries

CostMarshal v4.1's OCI controls isolate worker processes from
the host workspace, other provider credentials, mutable profiles, and scheduler
authority. They do not make the selected provider client hostile-safe.

Legacy raw-key OCI mode mounts the selected provider credential into the
worker. Code and model-directed tools in that container can read it, and
literal redaction cannot prevent encoded, transformed, or split exfiltration.
It is not admitted by an enforced production boundary. If legacy mode is used:

- use a dedicated, least-privilege, spend-capped, rate-limited, revocable key;
- trust the reviewed digest-pinned worker image and workload not to steal it;
- never reuse a broad personal or organization-wide credential;
- do not use the raw-key worker path for hostile workloads requiring credential
  confidentiality.

The v4.1 production gateway implements that stronger boundary. The Broker
authenticates an exact SPIFFE URI from an mTLS client certificate and issues a
signed, short-lived lease bound to provider, model, input/output token envelope,
budget, expiry, and reviewed policy hash. The Proxy alone reads the provider
key, atomically reserves integer nano-CNY in SQLite, rejects request-ID replay,
and settles provider usage conservatively. The Worker mounts only the lease and
the public CA bundle.

`production-status` probes both live TLS services and binds their health
responses to the reviewed policy hash. Enforced dispatch remains blocked when
the Broker/Proxy, worker digest/network, hard budget, SQLite authority, or
external evidence is absent. The single-host SQLite deployment must not be
placed on NFS or presented as multi-host HA.

Native worker mode is development compatibility only and is not a host security
boundary. ArchMarshal integration is read-only governance checking and does not
expand CostMarshal's authority.

## Certification

In the full source checkout, an empty `release/evidence-policy.json` means
external real-provider economics and malicious-OCI evidence have not been
certified. The curated installed-plugin snapshot intentionally omits that
maintainer-only policy. Release gates must not turn absent external evidence
into a production-certified claim.
