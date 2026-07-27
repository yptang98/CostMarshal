# Production gateway deployment

This deployment removes raw provider credentials from CostMarshal workers.
The mTLS broker issues a short-lived, provider/model/token/budget-scoped lease.
The provider proxy alone mounts the provider key and atomically reserves and
settles integer nano-CNY in SQLite.

`gateway-policy.example.json` is intentionally fail-closed: every zero price,
zero budget, and `REPLACE_...` identity must be replaced with reviewed
deployment values before it will validate.

## Required trust material

Keep all material outside the repository:

- a random lease-signing key of at least 32 bytes;
- broker and proxy TLS certificates and private keys;
- a client CA plus an mTLS worker certificate containing exactly one SPIFFE
  URI SAN matching the policy;
- one least-privilege, spend-capped provider-key file per enabled provider; and
- a digest-pinned gateway image built from
  [`container/gateway/Dockerfile`](../../container/gateway/Dockerfile).

The broker never mounts provider keys. The proxy never mounts the workload
client private key. The Worker receives only the short-lived lease token.

## Single-host deployment

1. Copy `gateway-policy.example.json` to `gateway-policy.json`, replace every
   fail-closed placeholder, and review the exact provider prices and model
   output limits. Validate it and record the returned immutable binding:

   `python scripts/costmarshal_gateway.py validate-policy --config deploy/production/gateway-policy.json`
2. Build and publish the gateway image from a digest-pinned Python base image.
3. Export the file-path variables required by `compose.yaml`; never put secret
   contents in the Compose file or project state. Pre-create the state and
   audit directories owned by uid/gid `65532`; the services run read-only and
   non-root and will not repair unsafe host permissions.
4. Set `COSTMARSHAL_GATEWAY_IMAGE` to the published image digest and run:

   `docker compose -f deploy/production/compose.yaml up -d`

5. Restrict the broker bind address to the scheduler host. Attach only managed
   Worker containers to the internal `costmarshal-provider-proxy` network.
6. Configure the CostMarshal project with
   `--runtime-adapter costmarshal-gateway-v1`,
   `--gateway-policy-sha256 sha256:<validated-hash>`, broker URL ending in
   `/v1/leases`, proxy URL ending in `/v1`, and enforced mode.
7. On the scheduler host, set these variables to the mTLS client files:

   - `COSTMARSHAL_BROKER_CLIENT_CERT_FILE`
   - `COSTMARSHAL_BROKER_CLIENT_KEY_FILE`
   - `COSTMARSHAL_BROKER_CA_FILE`

The shared SQLite volume is suitable for a single Docker host. Do not place it
on NFS or use this Compose topology as a multi-host HA database. A multi-host
deployment requires an external transactional ledger adapter.

## Fail-closed behavior

- A request without a valid signed lease is rejected.
- Provider/model/path/token-envelope drift is rejected.
- Budget is reserved with `BEGIN IMMEDIATE` before the upstream call.
- Reusing a request ID never repeats an upstream provider call.
- Unknown usage consumes the full reservation.
- A provider-reported overrun disables the lease.
- Provider credentials and lease tokens are excluded from audit records.
