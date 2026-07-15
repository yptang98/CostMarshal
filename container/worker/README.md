# CostMarshal worker image

This image is the trusted bootstrap for `worker_isolation.mode=required`. It contains two fixed commands:

- `costmarshal-isolation-canary --json` proves the non-root, read-only-rootfs, capability, mount, and engine-socket boundary before dispatch.
- `costmarshal-worker --jsonl [--model MODEL]` accepts the bounded task prompt on stdin, runs `codex exec --json`, and writes only `/out/final.md`.
- `costmarshal-escape-probe` is a test-only hostile workload used by `tests/oci_live_evidence.py`; normal dispatch never selects it.

Builds are intentionally fail-closed unless both the base image digest and Codex CLI version are explicit. A production build must be pushed to a reviewed registry so it receives a repository digest:

```powershell
docker buildx build container/worker `
  --platform linux/amd64 `
  --build-arg NODE_BASE_IMAGE=node@sha256:<reviewed-linux-image-digest> `
  --build-arg CODEX_NPM_VERSION=<reviewed-version> `
  --tag registry.example/costmarshal-worker:<reviewed-version> `
  --push
docker pull registry.example/costmarshal-worker:<reviewed-version>
docker image inspect registry.example/costmarshal-worker:<reviewed-version> --format '{{json .RepoDigests}}'
```

Record the returned immutable repository digest (`name@sha256:...`) in
`--worker-image` and in the reviewed `release/evidence-policy.json`; pull that
exact reference on every release-test host. A local-only mutable tag has no
`RepoDigest` and is intentionally insufficient. Record the base digest, Codex
version, registry provenance/signature, and SBOM in the release review. The
container receives the workspace, one credential at most, one credential-free
profile, and an empty output directory; it never receives the CostMarshal
runtime or the aggregate secrets file.
The image explicitly clears any base-image entrypoint so the inspected command is exactly the CostMarshal worker command selected by the adapter.

Run the live evidence harness with a locally available reviewed image:

```powershell
$env:COSTMARSHAL_OCI_IMAGE = "registry.example/costmarshal-worker@sha256:<digest>"
$env:COSTMARSHAL_OCI_PROVIDER_NETWORK = "costmarshal-provider-proxy"
$env:COSTMARSHAL_OCI_PROXY_CONTAINER = "reviewed-provider-proxy"
$env:COSTMARSHAL_OCI_PROXY_HEALTH_URL = "http://reviewed-provider-proxy/allowlisted-upstream-proof"
$env:COSTMARSHAL_OCI_PROXY_HEALTH_SHA256 = "<sha256-of-bounded-response-body>"
python tests/oci_live_evidence.py
```

The network must already be internal and CostMarshal-labelled. The proxy must be running, carry the same trust label, and be attached both to that internal network and an independently inspected non-internal egress network. The health URL must expose a credential-free response that the reviewed proxy policy obtains from an allowlisted upstream; its expected body hash prevents a local empty health page from satisfying the positive path. The harness then exercises read-only and writable workspaces, hostile output exchanges, mount/label/resource/security options, socket/runtime/aggregate-secret probes, direct egress denial, proxy reachability, immutable identity, and credential cleanup. It writes `artifacts/oci-attestation.json`; missing engine, image, health proof, or real proxy topology produces an explicit `blocked` result.
