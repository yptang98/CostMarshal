"""Cryptographically verified production deployment certification.

Runtime health and accepted project Artifacts are useful evidence, but neither
proves that an external reviewer certified the exact deployed release.  This
module verifies a short-lived OpenSSH detached signature over a canonical
deployment claim and binds every report hash to an accepted project Artifact.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any


PRODUCTION_CERTIFICATION_SCHEMA = "costmarshal-production-certification-v1"
PRODUCTION_CERTIFICATION_NAMESPACE = "costmarshal-production-certification-v1"
REQUIRED_PRODUCTION_EVIDENCE = frozenset(
    {
        "real_provider_backtest",
        "live_oci_adversarial",
        "credential_broker_attestation",
        "provider_proxy_budget_attestation",
        "schema_drift_monitor",
    }
)
MAX_CERTIFICATION_BYTES = 1024 * 1024
MAX_TRUST_FILE_BYTES = 1024 * 1024
MAX_SIGNATURE_BYTES = 64 * 1024
MAX_CERTIFICATION_LIFETIME = timedelta(days=7)

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_IMAGE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}\Z")
_VERSION = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?\Z")
_SIGNER = re.compile(r"[^\s\x00-\x1f]{1,256}\Z")


class ProductionEvidenceError(ValueError):
    """Raised when a production certification is missing, stale, or untrusted."""


@dataclass(frozen=True, slots=True)
class VerifiedProductionCertification:
    """Opaque receipt produced only after detached-signature verification."""

    _claim: Mapping[str, Any]
    claim_sha256: str
    signature_sha256: str
    allowed_signers_sha256: str
    signer_identity: str
    verified_at: str

    @property
    def claim(self) -> Mapping[str, Any]:
        return self._claim

    def public_receipt(self) -> dict[str, Any]:
        return {
            "schema_version": "costmarshal-production-certification-receipt-v1",
            "claim_sha256": self.claim_sha256,
            "signature_sha256": self.signature_sha256,
            "allowed_signers_sha256": self.allowed_signers_sha256,
            "signer_identity": self.signer_identity,
            "verified_at": self.verified_at,
            "git_sha": self._claim["git_sha"],
            "release_version": self._claim["release_version"],
            "boundary_sha256": self._claim["boundary_sha256"],
            "gateway_policy_sha256": self._claim["gateway_policy_sha256"],
            "worker_image": self._claim["worker_image"],
            "gateway_image": self._claim["gateway_image"],
            "expires_at": self._claim["expires_at"],
            "evidence": json.loads(json.dumps(self._claim["evidence"])),
        }


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProductionEvidenceError(
            "production certification must be canonical JSON"
        ) from exc


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _require_sha256(value: Any, label: str) -> str:
    result = str(value or "")
    if not _SHA256.fullmatch(result):
        raise ProductionEvidenceError(f"{label} must use sha256:<64 lowercase hex>")
    return result


def _utc_datetime(value: Any, label: str) -> datetime:
    text = str(value or "")
    if not text.endswith("Z"):
        raise ProductionEvidenceError(f"{label} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ProductionEvidenceError(
            f"{label} must be an RFC3339 UTC timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ProductionEvidenceError(f"{label} must be UTC")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProductionEvidenceError("certification time must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _evidence_rows(value: Mapping[str, Mapping[str, str]]) -> dict[str, dict[str, str]]:
    if set(value) != REQUIRED_PRODUCTION_EVIDENCE:
        raise ProductionEvidenceError(
            "production certification requires exactly the five external evidence types"
        )
    result: dict[str, dict[str, str]] = {}
    for evidence_type in sorted(REQUIRED_PRODUCTION_EVIDENCE):
        raw = value.get(evidence_type)
        if not isinstance(raw, Mapping) or set(raw) != {
            "artifact_id",
            "report_sha256",
        }:
            raise ProductionEvidenceError(
                f"{evidence_type} evidence must contain artifact_id and report_sha256"
            )
        artifact_id = str(raw.get("artifact_id") or "").strip()
        if not artifact_id or len(artifact_id) > 256 or any(
            ord(char) < 32 for char in artifact_id
        ):
            raise ProductionEvidenceError(
                f"{evidence_type} artifact_id is invalid"
            )
        result[evidence_type] = {
            "artifact_id": artifact_id,
            "report_sha256": _require_sha256(
                raw.get("report_sha256"),
                f"{evidence_type} report_sha256",
            ),
        }
    return result


def build_production_certification_claim(
    *,
    git_sha: str,
    release_version: str,
    boundary_sha256: str,
    gateway_policy_sha256: str,
    worker_image: str,
    gateway_image: str,
    evidence: Mapping[str, Mapping[str, str]],
    issued_at: datetime,
    expires_at: datetime,
) -> dict[str, Any]:
    """Build the exact canonical claim that an external reviewer signs."""

    claim = {
        "schema_version": PRODUCTION_CERTIFICATION_SCHEMA,
        "namespace": PRODUCTION_CERTIFICATION_NAMESPACE,
        "git_sha": str(git_sha or ""),
        "release_version": str(release_version or ""),
        "boundary_sha256": str(boundary_sha256 or ""),
        "gateway_policy_sha256": str(gateway_policy_sha256 or ""),
        "worker_image": str(worker_image or ""),
        "gateway_image": str(gateway_image or ""),
        "evidence": _evidence_rows(evidence),
        "issued_at": _utc_text(issued_at),
        "expires_at": _utc_text(expires_at),
    }
    return validate_production_certification_claim(claim)


def validate_production_certification_claim(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "namespace",
        "git_sha",
        "release_version",
        "boundary_sha256",
        "gateway_policy_sha256",
        "worker_image",
        "gateway_image",
        "evidence",
        "issued_at",
        "expires_at",
    }:
        raise ProductionEvidenceError(
            "production certification claim fields are invalid"
        )
    row = json.loads(json.dumps(dict(value)))
    if row.get("schema_version") != PRODUCTION_CERTIFICATION_SCHEMA:
        raise ProductionEvidenceError("unsupported production certification schema")
    if row.get("namespace") != PRODUCTION_CERTIFICATION_NAMESPACE:
        raise ProductionEvidenceError("production certification namespace is invalid")
    if not _GIT_SHA.fullmatch(str(row.get("git_sha") or "")):
        raise ProductionEvidenceError(
            "production certification git_sha must be an exact 40-hex commit"
        )
    if not _VERSION.fullmatch(str(row.get("release_version") or "")):
        raise ProductionEvidenceError(
            "production certification release_version is invalid"
        )
    _require_sha256(row.get("boundary_sha256"), "boundary_sha256")
    _require_sha256(row.get("gateway_policy_sha256"), "gateway_policy_sha256")
    for field in ("worker_image", "gateway_image"):
        if not _IMAGE.fullmatch(str(row.get(field) or "")):
            raise ProductionEvidenceError(
                f"production certification {field} must be digest-pinned"
            )
    evidence = row.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ProductionEvidenceError("production certification evidence is invalid")
    row["evidence"] = _evidence_rows(evidence)
    issued = _utc_datetime(row.get("issued_at"), "issued_at")
    expires = _utc_datetime(row.get("expires_at"), "expires_at")
    lifetime = expires - issued
    if lifetime <= timedelta(0) or lifetime > MAX_CERTIFICATION_LIFETIME:
        raise ProductionEvidenceError(
            "production certification lifetime must be positive and at most 7 days"
        )
    if _canonical_bytes(row) != _canonical_bytes(value):
        raise ProductionEvidenceError("production certification claim is not canonical")
    return row


def canonical_certification_bytes(value: Mapping[str, Any]) -> bytes:
    return _canonical_bytes(validate_production_certification_claim(value))


def _read_bounded(path: Path, *, limit: int, label: str) -> bytes:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ProductionEvidenceError(f"{label} must not be a symlink")
    try:
        stat = candidate.stat()
    except OSError as exc:
        raise ProductionEvidenceError(f"{label} is unavailable") from exc
    if not candidate.is_file() or stat.st_size <= 0 or stat.st_size > limit:
        raise ProductionEvidenceError(f"{label} size is invalid")
    try:
        data = candidate.read_bytes()
    except OSError as exc:
        raise ProductionEvidenceError(f"{label} is unreadable") from exc
    if len(data) != stat.st_size:
        raise ProductionEvidenceError(f"{label} changed while being read")
    return data


def _latest_artifacts(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    latest: dict[str, Mapping[str, Any]] = {}
    for raw in rows:
        artifact_id = raw.get("artifact_id")
        if isinstance(artifact_id, str) and artifact_id:
            latest[artifact_id] = raw
    return latest


def _verify_artifact_bindings(
    claim: Mapping[str, Any],
    artifact_rows: Iterable[Mapping[str, Any]],
) -> None:
    latest = _latest_artifacts(artifact_rows)
    for evidence_type, binding in claim["evidence"].items():
        artifact_id = binding["artifact_id"]
        artifact = latest.get(artifact_id)
        storage = artifact.get("storage") if isinstance(artifact, Mapping) else None
        if (
            not isinstance(artifact, Mapping)
            or artifact.get("schema_version") != "costmarshal-project-artifact-v1"
            or artifact.get("lifecycle") != "accepted"
            or artifact.get("kind") != "production-evidence"
            or not isinstance(storage, Mapping)
            or storage.get("sha256") != binding["report_sha256"]
        ):
            raise ProductionEvidenceError(
                f"{evidence_type} is not bound to an accepted production-evidence Artifact receipt"
            )


def verify_production_certification(
    *,
    manifest_path: Path,
    signature_path: Path,
    allowed_signers_path: Path,
    signer_identities: Iterable[str],
    expected_allowed_signers_sha256: str,
    expected_git_sha: str,
    expected_release_version: str,
    expected_boundary_sha256: str,
    expected_gateway_policy_sha256: str,
    expected_worker_image: str,
    expected_gateway_image: str,
    artifact_rows: Iterable[Mapping[str, Any]],
    now: datetime | None = None,
    ssh_keygen: str | None = None,
) -> VerifiedProductionCertification:
    """Verify trust root, signature, expiry, deployment, and Artifact receipts."""

    manifest = _read_bounded(
        Path(manifest_path),
        limit=MAX_CERTIFICATION_BYTES,
        label="production certification manifest",
    )
    signature = _read_bounded(
        Path(signature_path),
        limit=MAX_SIGNATURE_BYTES,
        label="production certification signature",
    )
    allowed_signers = _read_bounded(
        Path(allowed_signers_path),
        limit=MAX_TRUST_FILE_BYTES,
        label="production certification allowed_signers",
    )
    trust_hash = _sha256_bytes(allowed_signers)
    if trust_hash != _require_sha256(
        expected_allowed_signers_sha256,
        "expected allowed_signers sha256",
    ):
        raise ProductionEvidenceError(
            "production certification trust root does not match the reviewed hash"
        )
    try:
        decoded = manifest.decode("utf-8")
        parsed = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductionEvidenceError(
            "production certification manifest is not UTF-8 JSON"
        ) from exc
    claim = validate_production_certification_claim(parsed)
    if manifest != _canonical_bytes(claim):
        raise ProductionEvidenceError(
            "production certification manifest bytes are not canonical"
        )
    expected = {
        "git_sha": expected_git_sha,
        "release_version": expected_release_version,
        "boundary_sha256": expected_boundary_sha256,
        "gateway_policy_sha256": expected_gateway_policy_sha256,
        "worker_image": expected_worker_image,
        "gateway_image": expected_gateway_image,
    }
    for field, expected_value in expected.items():
        if claim.get(field) != expected_value:
            raise ProductionEvidenceError(
                f"production certification {field} does not match the deployment"
            )
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ProductionEvidenceError("verification time must include a timezone")
    current = current.astimezone(timezone.utc)
    issued = _utc_datetime(claim["issued_at"], "issued_at")
    expires = _utc_datetime(claim["expires_at"], "expires_at")
    if current < issued - timedelta(minutes=5):
        raise ProductionEvidenceError("production certification is not yet valid")
    if current >= expires:
        raise ProductionEvidenceError("production certification has expired")
    _verify_artifact_bindings(claim, artifact_rows)

    identities = list(dict.fromkeys(str(item or "").strip() for item in signer_identities))
    if not identities or any(not _SIGNER.fullmatch(item) for item in identities):
        raise ProductionEvidenceError(
            "production certification signer identities are invalid"
        )
    executable = ssh_keygen or shutil.which("ssh-keygen")
    if not executable:
        raise ProductionEvidenceError(
            "ssh-keygen is unavailable; production certification cannot be verified"
        )
    matched: str | None = None
    with tempfile.TemporaryDirectory(prefix="costmarshal-production-cert-") as raw:
        frozen = Path(raw)
        trust_file = frozen / "allowed_signers"
        signature_file = frozen / "claim.sig"
        trust_file.write_bytes(allowed_signers)
        signature_file.write_bytes(signature)
        for identity in identities:
            try:
                result = subprocess.run(
                    [
                        executable,
                        "-Y",
                        "verify",
                        "-f",
                        str(trust_file),
                        "-I",
                        identity,
                        "-n",
                        PRODUCTION_CERTIFICATION_NAMESPACE,
                        "-s",
                        str(signature_file),
                    ],
                    input=manifest,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ProductionEvidenceError(
                    "production certification signature verification could not run"
                ) from exc
            if result.returncode == 0:
                matched = identity
                break
    if matched is None:
        raise ProductionEvidenceError(
            "production certification signature is not trusted"
        )
    frozen_claim = MappingProxyType(json.loads(json.dumps(claim)))
    return VerifiedProductionCertification(
        _claim=frozen_claim,
        claim_sha256=_sha256_bytes(manifest),
        signature_sha256=_sha256_bytes(signature),
        allowed_signers_sha256=trust_hash,
        signer_identity=matched,
        verified_at=_utc_text(current),
    )


__all__ = [
    "MAX_CERTIFICATION_LIFETIME",
    "PRODUCTION_CERTIFICATION_NAMESPACE",
    "PRODUCTION_CERTIFICATION_SCHEMA",
    "REQUIRED_PRODUCTION_EVIDENCE",
    "ProductionEvidenceError",
    "VerifiedProductionCertification",
    "build_production_certification_claim",
    "canonical_certification_bytes",
    "validate_production_certification_claim",
    "verify_production_certification",
]
