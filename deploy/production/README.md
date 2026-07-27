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
- one least-privilege, spend-capped provider-key file per enabled provider,
  stored as a direct child of a Proxy-only credential directory; and
- a digest-pinned gateway image built from
  [`container/gateway/Dockerfile`](../../container/gateway/Dockerfile);
- an external Ed25519 production-certification signing key; and
- a reviewed OpenSSH `allowed_signers` file. Only its SHA-256 and signer
  identities enter the project boundary; the private key never enters
  CostMarshal.

The broker never mounts provider keys. The proxy never mounts the workload
client private key. The Worker receives only the short-lived lease token.

## Single-host deployment

1. Copy `gateway-policy.example.json` to `gateway-policy.json`, replace every
   fail-closed placeholder, and review the exact provider prices and model
   output limits. Set `wire_api` to `responses`, `chat-completions`, or `both`
   and list only verified `input_modalities`. The Chat adapter supports
   Responses text, image, audio, function tools, JSON, and buffered SSE;
   video/document require native Responses. Validate the policy and record the
   returned immutable binding:

   `python scripts/costmarshal_gateway.py validate-policy --config deploy/production/gateway-policy.json`

   Common reviewed preset mapping:

   | Provider | `wire_api` | Maximum gateway input transport |
   | --- | --- | --- |
   | DeepSeek | `chat-completions` | text |
   | Kimi | `chat-completions` | text, image, audio where the selected model documents it |
   | LongCat | `responses` for a deployment-verified endpoint; otherwise `chat-completions` | text |
   | MiMo | `responses` | text, image, audio, video where the selected model documents it |
   | Doubao | `responses` | text, image, audio, video where the selected model documents it |

   Do not copy capabilities between models. `provider-presets` records the
   reviewed model-specific API facts, while gateway policy must contain only
   modalities verified for the exact deployed endpoint.
2. Build and publish the gateway image from a digest-pinned Python base image.
3. Export the file-path variables required by `compose.yaml`; never put secret
   contents in the Compose file or project state. Set
   `COSTMARSHAL_PROVIDER_CREDENTIALS_DIR` to a non-symlink directory containing
   the exact credential basenames referenced as
   `/run/provider-credentials/<name>` in policy. Only the Proxy mounts this
   directory. Pre-create the state and audit directories owned by uid/gid
   `65532`; the services run read-only and non-root and will not repair unsafe
   host permissions.
4. Set `COSTMARSHAL_GATEWAY_IMAGE` to the published image digest. Run the
   read-only preflight first; it validates the clean release commit, policy,
   image digest, Docker/Compose, secret file bounds, credential mapping, and
   directory ownership without printing secret contents:

   `python scripts/costmarshal_production_deploy.py --output artifacts/deployment-preflight.json`

   After reviewing the receipt, explicitly deploy:

   `python scripts/costmarshal_production_deploy.py --apply --output artifacts/deployment-preflight.json`

5. Restrict the broker bind address to the scheduler host. Attach only managed
   Worker containers to the internal `costmarshal-provider-proxy` network.
6. Configure the CostMarshal project with
   `--runtime-adapter costmarshal-gateway-v1`,
   `--gateway-policy-sha256 sha256:<validated-hash>`, broker URL ending in
   `/v1/leases`, proxy URL ending in `/v1`, enforced mode, and these immutable
   certification bindings:

   - `--deployment-commit <40-hex>`
   - `--release-version v4.3.0`
   - `--gateway-image name@sha256:<64-hex>`
   - `--allowed-signers-sha256 sha256:<64-hex>`
   - `--signer-identity <reviewed-identity>` (repeatable)

7. On the scheduler host, set these variables to the mTLS client files:

   - `COSTMARSHAL_BROKER_CLIENT_CERT_FILE`
   - `COSTMARSHAL_BROKER_CLIENT_KEY_FILE`
   - `COSTMARSHAL_BROKER_CA_FILE`

8. Exercise the deployed Broker, Proxy, and selected real provider. The harness
   verifies live policy-bound health, idempotent lease issuance, one settled
   real call with usage, request replay rejection, and token-cap enforcement
   without writing the lease or provider key into evidence:

   `python tests/release/run_gateway_evidence.py --broker-endpoint <https://.../v1/leases> --proxy-endpoint <https://.../v1> --policy-sha256 <sha256:...> --client-cert <path> --client-key <path> --ca <path> --provider <id> --model <id> --budget-nano-cny <integer>`

9. Run the Provider schema-drift canary with the same reviewed endpoint and
   exact model:

   `python tests/release/run_provider_drift_evidence.py --broker-endpoint <https://.../v1/leases> --proxy-endpoint <https://.../v1> --policy-sha256 <sha256:...> --client-cert <path> --client-key <path> --ca <path> --provider <id> --model <id> --budget-nano-cny <integer>`

   The report stores response hashes and normalized field/type shapes, never
   response content, credentials, or leases. Review and register it as the
   `schema_drift_monitor` production-evidence Artifact.

10. Register the five external reports as accepted `production-evidence`
   project Artifacts. The gateway runtime report can support the Broker and
   Proxy attestation types, while the real-provider matrix, malicious OCI run,
   and schema-drift monitor remain separate evidence. Generate an
   at-most-seven-day canonical claim:

   `python scripts/costmarshal_production_certification.py create --boundary <production-boundary.json> --artifacts <artifacts.jsonl> --worker-image <worker@sha256:...> --output <production-certification.json>`

11. On the independent review/signing host, sign the exact manifest bytes:

   `ssh-keygen -Y sign -f <external-ed25519-key> -n costmarshal-production-certification-v1 <production-certification.json>`

12. Mount the claim, detached signature, and reviewed public trust file
    read-only into the scheduler, then set:

    - `COSTMARSHAL_PRODUCTION_CERTIFICATION_FILE`
    - `COSTMARSHAL_PRODUCTION_CERTIFICATION_SIGNATURE_FILE`
    - `COSTMARSHAL_PRODUCTION_ALLOWED_SIGNERS_FILE`

13. Run `production-status`. `runtime_status=ready` proves the live gateway
    bindings; `certification_status=certified` proves the external signature.
    Only their conjunction returns final `status=ready`.

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
- Healthy services or accepted Artifact IDs without a trusted, unexpired
  deployment signature never produce external certification.
