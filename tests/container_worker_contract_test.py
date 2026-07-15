#!/usr/bin/env python3
"""Static release contract for the digest-pinned CostMarshal worker image."""

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "container" / "worker" / "costmarshal-worker.js"
CANARY = ROOT / "container" / "worker" / "costmarshal-isolation-canary.js"
PROBE = ROOT / "container" / "worker" / "costmarshal-escape-probe.js"
DOCKERFILE = ROOT / "container" / "worker" / "Dockerfile"
LIVE_HARNESS = ROOT / "tests" / "oci_live_evidence.py"


def main() -> int:
    for script in (WORKER, CANARY, PROBE):
        completed = subprocess.run(
            ["node", "--check", str(script)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
    worker = WORKER.read_text(encoding="utf-8")
    canary = CANARY.read_text(encoding="utf-8")
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    for required in (
        'path.resolve(value) !== expected',
        '"--ask-for-approval"',
        '"--ephemeral"',
        '"--skip-git-repo-check"',
        '"--json"',
        '"--output-last-message"',
        'shell: false',
        'fs.existsSync(output)',
    ):
        assert required in worker
    assert "eval(" not in worker and "exec(" not in worker
    assert "provider-secret" not in worker
    for required in (
        "no_new_privileges",
        "rootfs_write_blocked",
        "workspace_writable",
        "runtime_visible",
        "aggregate_secrets_visible",
        "engine_socket_visible",
    ):
        assert required in canary
    assert "ARG NODE_BASE_IMAGE" in dockerfile
    assert "FROM ${NODE_BASE_IMAGE}" in dockerfile
    assert "ARG CODEX_NPM_VERSION" in dockerfile
    assert "COPY costmarshal-escape-probe.js" in dockerfile
    assert '@openai/codex@${CODEX_NPM_VERSION}' in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert "ENTRYPOINT []" in dockerfile
    assert ":latest" not in dockerfile
    harness = LIVE_HARNESS.read_text(encoding="utf-8")
    for required in (
        "artifacts\" / \"oci-attestation.json",
        "mount_allowlist_excludes_runtime_and_aggregate",
        "symlink-output",
        "extra-output",
        "oversize-output",
        "credential_cleanup",
        "network_policy",
    ):
        assert required in harness
    print("container worker contract ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
