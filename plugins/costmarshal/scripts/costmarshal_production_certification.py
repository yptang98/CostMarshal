#!/usr/bin/env python3
"""Create and verify short-lived signed production certification claims."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from costmarshal_v2.large_project import (  # noqa: E402
    PRODUCTION_BOUNDARY_SCHEMA,
    validate_production_boundary,
)
from costmarshal_v2.production_evidence import (  # noqa: E402
    ProductionEvidenceError,
    build_production_certification_claim,
    canonical_certification_bytes,
    verify_production_certification,
)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionEvidenceError(f"could not read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ProductionEvidenceError(f"JSON root must be an object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ProductionEvidenceError(
            f"could not read Artifact ledger: {path}"
        ) from exc
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProductionEvidenceError(
                f"Artifact ledger line {index} is invalid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise ProductionEvidenceError(
                f"Artifact ledger line {index} must be an object"
            )
        rows.append(value)
    return rows


def latest_artifacts(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        artifact_id = row.get("artifact_id")
        if isinstance(artifact_id, str) and artifact_id:
            result[artifact_id] = row
    return result


def claim_evidence(
    boundary: dict[str, Any],
    artifact_rows: list[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    latest = latest_artifacts(artifact_rows)
    result: dict[str, dict[str, str]] = {}
    for evidence_type, artifact_id in boundary["evidence_artifact_ids"].items():
        artifact = latest.get(artifact_id)
        storage = artifact.get("storage") if isinstance(artifact, dict) else None
        if (
            not isinstance(artifact, dict)
            or artifact.get("schema_version") != "costmarshal-project-artifact-v1"
            or artifact.get("lifecycle") != "accepted"
            or artifact.get("kind") != "production-evidence"
            or not isinstance(storage, dict)
            or not isinstance(storage.get("sha256"), str)
        ):
            raise ProductionEvidenceError(
                f"{evidence_type} must be an accepted production-evidence Artifact receipt"
            )
        result[evidence_type] = {
            "artifact_id": artifact_id,
            "report_sha256": storage["sha256"],
        }
    return result


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def create_claim(args: argparse.Namespace) -> dict[str, Any]:
    boundary = validate_production_boundary(read_json(args.boundary))
    if boundary.get("schema_version") != PRODUCTION_BOUNDARY_SCHEMA:
        raise ProductionEvidenceError(
            "signed certification requires a v3 production boundary"
        )
    rows = read_jsonl(args.artifacts)
    runtime = boundary["runtime_adapter_config"]
    now = datetime.now(timezone.utc)
    if args.valid_hours <= 0 or args.valid_hours > 168:
        raise ProductionEvidenceError("--valid-hours must be in the range 1..168")
    claim = build_production_certification_claim(
        git_sha=runtime["deployment_commit"],
        release_version=runtime["release_version"],
        boundary_sha256=boundary["boundary_sha256"],
        gateway_policy_sha256=runtime["policy_sha256"],
        worker_image=args.worker_image,
        gateway_image=runtime["gateway_image"],
        evidence=claim_evidence(boundary, rows),
        issued_at=now,
        expires_at=now + timedelta(hours=args.valid_hours),
    )
    payload = canonical_certification_bytes(claim)
    atomic_write(args.output, payload)
    return {
        "status": "created",
        "manifest": str(args.output.resolve()),
        "bytes": len(payload),
        "boundary_sha256": boundary["boundary_sha256"],
        "expires_at": claim["expires_at"],
        "next_command": (
            "ssh-keygen -Y sign -f <external-ed25519-private-key> "
            " -n costmarshal-production-certification-v1 "
            f"{args.output.resolve()}"
        ),
    }


def verify_claim(args: argparse.Namespace) -> dict[str, Any]:
    boundary = validate_production_boundary(read_json(args.boundary))
    if boundary.get("schema_version") != PRODUCTION_BOUNDARY_SCHEMA:
        raise ProductionEvidenceError(
            "signed certification requires a v3 production boundary"
        )
    rows = read_jsonl(args.artifacts)
    runtime = boundary["runtime_adapter_config"]
    trust = boundary["certification_trust"]
    receipt = verify_production_certification(
        manifest_path=args.manifest,
        signature_path=args.signature,
        allowed_signers_path=args.allowed_signers,
        signer_identities=trust["signer_identities"],
        expected_allowed_signers_sha256=trust["allowed_signers_sha256"],
        expected_git_sha=runtime["deployment_commit"],
        expected_release_version=runtime["release_version"],
        expected_boundary_sha256=boundary["boundary_sha256"],
        expected_gateway_policy_sha256=runtime["policy_sha256"],
        expected_worker_image=args.worker_image,
        expected_gateway_image=runtime["gateway_image"],
        artifact_rows=rows,
    )
    return {"status": "verified", "receipt": receipt.public_receipt()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or verify a CostMarshal production certification"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="create canonical JSON for external signing")
    create.add_argument("--boundary", type=Path, required=True)
    create.add_argument("--artifacts", type=Path, required=True)
    create.add_argument("--worker-image", required=True)
    create.add_argument("--valid-hours", type=int, default=24)
    create.add_argument("--output", type=Path, required=True)
    create.set_defaults(func=create_claim)

    verify = sub.add_parser("verify", help="verify exact deployment and signature")
    verify.add_argument("--boundary", type=Path, required=True)
    verify.add_argument("--artifacts", type=Path, required=True)
    verify.add_argument("--worker-image", required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--signature", type=Path, required=True)
    verify.add_argument("--allowed-signers", type=Path, required=True)
    verify.set_defaults(func=verify_claim)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = args.func(args)
    except ProductionEvidenceError as exc:
        print(
            json.dumps(
                {"status": "blocked", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
