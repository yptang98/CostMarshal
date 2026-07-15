"""Read-only ArchMarshal governance discovery and binding validation.

This module deliberately has no ArchMarshal import and no wrapper discovery.
Callers must provide the exact reviewed ``invoke_archmarshal.py`` path.  The
only supported subprocess operations are the dependency-free bootstrap identity
check and the read-only workspace doctor.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


GOVERNANCE_MODES = frozenset({"off", "auto", "required"})
BINDING_FORMAT = "costmarshal-archmarshal-binding-v1"
MAX_WRAPPER_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_OWNERSHIP_BYTES = 64 * 1024
MAX_SKILL_HEAD_BYTES = 1024
_BINDING_FIELDS = (
    "format",
    "provider",
    "engine_api",
    "engine_version",
    "engine_source_sha256",
    "workspace_root",
    "ownership_marker_sha256",
    "skill_index_head",
)


class GovernanceError(RuntimeError):
    """A fail-closed governance error with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def inspect_governance(
    workspace: Path | str,
    *,
    mode: str = "auto",
    wrapper_path: Path | str | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Inspect an ArchMarshal workspace without writing to it.

    ``off`` performs no wrapper or workspace reads. ``auto`` converts an
    unavailable or not-ready integration into a warning. ``required`` raises a
    :class:`GovernanceError` unless the verified engine and doctor both report a
    healthy, read-only workspace with a usable ownership marker.
    """

    normalized_mode = _governance_mode(mode)
    if normalized_mode == "off":
        return {
            "mode": "off",
            "status": "off",
            "ready": False,
            "doctor_state": None,
            "warnings": [],
            "binding": None,
        }

    try:
        workspace_path = _workspace_path(workspace)
        wrapper = _explicit_wrapper_path(wrapper_path)
        bootstrap = _invoke_wrapper(wrapper, ["--bootstrap-status"], timeout_seconds)
        bootstrap_identity = _bootstrap_identity(bootstrap)
        doctor = _invoke_wrapper(wrapper, ["doctor", str(workspace_path)], timeout_seconds)
        doctor_state, doctor_issues = _doctor_readiness(doctor, workspace_path)
        workspace_identity, identity_issues = _workspace_identity(workspace_path)
        issues = [*doctor_issues, *identity_issues]
        binding = _make_binding(bootstrap_identity, workspace_path, workspace_identity)
        if doctor_state != "healthy":
            issues.insert(
                0,
                {
                    "code": "archmarshal_workspace_not_ready",
                    "message": f"ArchMarshal doctor state is {doctor_state!r}, not 'healthy'.",
                },
            )
        if issues:
            return _not_ready(normalized_mode, doctor_state, binding, issues)
        return {
            "mode": normalized_mode,
            "status": "ready",
            "ready": True,
            "doctor_state": doctor_state,
            "warnings": [],
            "binding": binding,
        }
    except GovernanceError as exc:
        if normalized_mode == "required":
            raise
        return {
            "mode": normalized_mode,
            "status": "warning",
            "ready": False,
            "doctor_state": None,
            "warnings": [{"code": exc.code, "message": str(exc)}],
            "binding": None,
        }


def validate_governance_binding(
    binding: dict[str, Any],
    workspace: Path | str,
    *,
    mode: str = "required",
    wrapper_path: Path | str | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Re-inspect a workspace and compare its stable governance identity."""

    normalized_mode = _governance_mode(mode)
    if normalized_mode == "off":
        return {
            "mode": "off",
            "status": "off",
            "ready": False,
            "valid": True,
            "drift": [],
            "warnings": [],
            "binding": None,
        }
    if not isinstance(binding, dict):
        return _binding_failure(
            normalized_mode,
            "governance_binding_invalid",
            "Governance binding must be a JSON object.",
        )

    inspection = inspect_governance(
        workspace,
        mode=normalized_mode,
        wrapper_path=wrapper_path,
        timeout_seconds=timeout_seconds,
    )
    current = inspection.get("binding")
    if not isinstance(current, dict):
        return _binding_failure(
            normalized_mode,
            "governance_binding_unavailable",
            "Current governance identity is unavailable.",
            warnings=inspection.get("warnings"),
        )

    drift = [
        {
            "field": field,
            "expected": binding.get(field),
            "actual": current.get(field),
        }
        for field in _BINDING_FIELDS
        if binding.get(field) != current.get(field)
    ]
    if drift:
        message = "ArchMarshal governance identity changed after the binding was recorded."
        if normalized_mode == "required":
            raise GovernanceError(
                "governance_binding_drift",
                message,
                details={"drift": drift},
            )
        warnings = list(inspection.get("warnings") or [])
        warnings.append({"code": "governance_binding_drift", "message": message})
        return {
            "mode": normalized_mode,
            "status": "warning",
            "ready": False,
            "valid": False,
            "drift": drift,
            "warnings": warnings,
            "binding": current,
        }
    return {
        "mode": normalized_mode,
        "status": inspection["status"],
        "ready": bool(inspection["ready"]),
        "valid": bool(inspection["ready"]),
        "drift": [],
        "warnings": list(inspection.get("warnings") or []),
        "binding": current,
    }


def _governance_mode(mode: str) -> str:
    normalized = str(mode or "").strip().lower()
    if normalized not in GOVERNANCE_MODES:
        raise GovernanceError(
            "governance_mode_invalid",
            f"Governance mode must be one of: {', '.join(sorted(GOVERNANCE_MODES))}.",
        )
    return normalized


def _workspace_path(workspace: Path | str) -> Path:
    try:
        path = Path(workspace).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GovernanceError(
            "governance_workspace_unavailable",
            "Governance workspace does not resolve to an existing directory.",
        ) from exc
    if not path.is_dir():
        raise GovernanceError(
            "governance_workspace_unavailable",
            "Governance workspace does not resolve to an existing directory.",
        )
    return path


def _explicit_wrapper_path(wrapper_path: Path | str | None) -> Path:
    if wrapper_path is None or not str(wrapper_path).strip():
        raise GovernanceError(
            "archmarshal_wrapper_required",
            "An explicit reviewed ArchMarshal wrapper path is required.",
        )
    try:
        path = Path(wrapper_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GovernanceError(
            "archmarshal_wrapper_unavailable",
            "The explicit ArchMarshal wrapper path is unavailable.",
        ) from exc
    if not path.is_file():
        raise GovernanceError(
            "archmarshal_wrapper_unavailable",
            "The explicit ArchMarshal wrapper path is not a file.",
        )
    return path


def _invoke_wrapper(wrapper: Path, arguments: list[str], timeout_seconds: float) -> dict[str, Any]:
    if timeout_seconds <= 0:
        raise GovernanceError(
            "archmarshal_timeout_invalid",
            "ArchMarshal timeout must be positive.",
        )
    try:
        completed = subprocess.run(
            [sys.executable, str(wrapper), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GovernanceError(
            "archmarshal_wrapper_failed",
            "The explicit ArchMarshal wrapper could not complete its read-only check.",
        ) from exc
    if completed.returncode != 0:
        raise GovernanceError(
            "archmarshal_wrapper_failed",
            "The explicit ArchMarshal wrapper rejected its read-only check.",
            details={"returncode": completed.returncode},
        )
    raw = completed.stdout
    if len(raw.encode("utf-8")) > MAX_WRAPPER_OUTPUT_BYTES:
        raise GovernanceError(
            "archmarshal_output_too_large",
            "ArchMarshal read-only output exceeded the bounded response size.",
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GovernanceError(
            "archmarshal_output_invalid",
            "ArchMarshal read-only output was not one JSON object.",
        ) from exc
    if not isinstance(payload, dict):
        raise GovernanceError(
            "archmarshal_output_invalid",
            "ArchMarshal read-only output was not one JSON object.",
        )
    return payload


def _bootstrap_identity(payload: dict[str, Any]) -> dict[str, str]:
    source_hash = payload.get("source_tree_sha256")
    engine_api = payload.get("engine_api")
    engine_version = payload.get("engine_version")
    if (
        payload.get("api_version") != "archmarshal-plugin-bootstrap-v2"
        or payload.get("verified") is not True
        or payload.get("mode") != "ready"
        or not isinstance(engine_api, str)
        or not engine_api
        or not isinstance(engine_version, str)
        or not engine_version
        or not _is_sha256(source_hash)
    ):
        raise GovernanceError(
            "archmarshal_bootstrap_unverified",
            "ArchMarshal bootstrap identity is missing, mismatched, or unverified.",
        )
    return {
        "engine_api": engine_api,
        "engine_version": engine_version,
        "engine_source_sha256": source_hash,
    }


def _doctor_readiness(
    payload: dict[str, Any], workspace: Path
) -> tuple[str, list[dict[str, str]]]:
    state = payload.get("state")
    issues: list[dict[str, str]] = []
    if payload.get("api_version") != "archmarshal-cli-v1":
        issues.append(
            {
                "code": "archmarshal_doctor_api_invalid",
                "message": "ArchMarshal doctor returned an unsupported API envelope.",
            }
        )
    if payload.get("mode") != "read_only" or payload.get("source_mutation") is not False:
        issues.append(
            {
                "code": "archmarshal_doctor_not_read_only",
                "message": "ArchMarshal doctor did not attest a read-only, non-mutating check.",
            }
        )
    doctor_root = payload.get("workspace_root")
    if not isinstance(doctor_root, str) or not _same_path(doctor_root, workspace):
        issues.append(
            {
                "code": "archmarshal_workspace_mismatch",
                "message": "ArchMarshal doctor reported a different workspace root.",
            }
        )
    if state not in {"healthy", "absent", "warning", "error"}:
        issues.append(
            {
                "code": "archmarshal_doctor_state_invalid",
                "message": "ArchMarshal doctor returned an unknown workspace state.",
            }
        )
        state = "error"
    summary = payload.get("summary")
    if not isinstance(summary, dict) or not isinstance(summary.get("error"), int):
        issues.append(
            {
                "code": "archmarshal_doctor_summary_invalid",
                "message": "ArchMarshal doctor returned no bounded severity summary.",
            }
        )
    elif summary["error"] != 0:
        issues.append(
            {
                "code": "archmarshal_doctor_errors",
                "message": "ArchMarshal doctor reported workspace errors.",
            }
        )
    blocking = sorted(
        {
            str(item.get("classification"))
            for item in payload.get("findings", [])
            if isinstance(item, dict)
            and item.get("classification")
            in {"corrupt", "partial", "partial_package", "incomplete", "unsafe"}
        }
    )
    if blocking:
        issues.append(
            {
                "code": "archmarshal_doctor_blocking_state",
                "message": "ArchMarshal doctor retained blocking state: " + ", ".join(blocking) + ".",
            }
        )
    return str(state), issues


def _workspace_identity(workspace: Path) -> tuple[dict[str, str | None], list[dict[str, str]]]:
    marker_path = workspace / ".agent" / "ownership.json"
    marker_hash, marker, marker_issue = _ownership_marker(marker_path)
    issues: list[dict[str, str]] = []
    if marker_issue:
        issues.append(marker_issue)

    head_path = workspace / ".agent" / "skill-overlays" / ".archmarshal" / "HEAD"
    skill_head, head_issue = _skill_head(head_path)
    if head_issue:
        issues.append(head_issue)
    index_mode = marker.get("skill_index") if isinstance(marker, dict) else None
    if index_mode == "required" and skill_head is None:
        issues.append(
            {
                "code": "archmarshal_skill_head_required",
                "message": "ArchMarshal ownership requires a valid Skill index HEAD.",
            }
        )
    return {
        "ownership_marker_sha256": marker_hash,
        "skill_index_head": skill_head,
    }, issues


def _ownership_marker(
    path: Path,
) -> tuple[str | None, dict[str, Any] | None, dict[str, str] | None]:
    if not path.exists():
        return None, None, {
            "code": "archmarshal_ownership_absent",
            "message": "ArchMarshal ownership marker is absent.",
        }
    if path.is_symlink() or not path.is_file():
        return None, None, {
            "code": "archmarshal_ownership_unsafe",
            "message": "ArchMarshal ownership marker is not a regular file.",
        }
    try:
        if path.stat().st_size > MAX_OWNERSHIP_BYTES:
            return None, None, {
                "code": "archmarshal_ownership_invalid",
                "message": "ArchMarshal ownership marker exceeds the bounded size.",
            }
        raw = path.read_bytes()
    except OSError:
        return None, None, {
            "code": "archmarshal_ownership_unreadable",
            "message": "ArchMarshal ownership marker could not be read safely.",
        }
    marker_hash = hashlib.sha256(raw).hexdigest()
    if len(raw) > MAX_OWNERSHIP_BYTES:
        return None, None, {
            "code": "archmarshal_ownership_invalid",
            "message": "ArchMarshal ownership marker exceeds the bounded size.",
        }
    try:
        marker = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return marker_hash, None, {
            "code": "archmarshal_ownership_invalid",
            "message": "ArchMarshal ownership marker is invalid JSON.",
        }
    if not (
        isinstance(marker, dict)
        and marker.get("format") == "archmarshal-workspace-ownership-v1"
        and isinstance(marker.get("workspace_id"), str)
        and bool(marker["workspace_id"])
        and marker.get("managed_root") == "."
        and marker.get("skill_index") in {"required", "disabled"}
        and marker.get("source_mutation") is False
    ):
        return marker_hash, marker if isinstance(marker, dict) else None, {
            "code": "archmarshal_ownership_invalid",
            "message": "ArchMarshal ownership marker has an invalid bounded structure.",
        }
    return marker_hash, marker, None


def _skill_head(path: Path) -> tuple[str | None, dict[str, str] | None]:
    if not path.exists():
        return None, None
    if path.is_symlink() or not path.is_file():
        return None, {
            "code": "archmarshal_skill_head_unsafe",
            "message": "ArchMarshal Skill index HEAD is not a regular file.",
        }
    try:
        if path.stat().st_size > MAX_SKILL_HEAD_BYTES:
            return None, {
                "code": "archmarshal_skill_head_invalid",
                "message": "ArchMarshal Skill index HEAD exceeds the bounded size.",
            }
        raw = path.read_bytes()
    except OSError:
        return None, {
            "code": "archmarshal_skill_head_unreadable",
            "message": "ArchMarshal Skill index HEAD could not be read safely.",
        }
    if len(raw) > MAX_SKILL_HEAD_BYTES:
        return None, {
            "code": "archmarshal_skill_head_invalid",
            "message": "ArchMarshal Skill index HEAD exceeds the bounded size.",
        }
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        value = ""
    if not _is_sha256(value):
        return None, {
            "code": "archmarshal_skill_head_invalid",
            "message": "ArchMarshal Skill index HEAD is not a SHA-256 digest.",
        }
    return value, None


def _make_binding(
    bootstrap: dict[str, str],
    workspace: Path,
    identity: dict[str, str | None],
) -> dict[str, Any]:
    return {
        "format": BINDING_FORMAT,
        "provider": "archmarshal",
        **bootstrap,
        "workspace_root": str(workspace),
        "ownership_marker_sha256": identity["ownership_marker_sha256"],
        "skill_index_head": identity["skill_index_head"],
        "captured_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }


def _not_ready(
    mode: str,
    doctor_state: str,
    binding: dict[str, Any],
    issues: list[dict[str, str]],
) -> dict[str, Any]:
    if mode == "required":
        raise GovernanceError(
            "archmarshal_governance_not_ready",
            "Required ArchMarshal governance is not ready.",
            details={
                "doctor_state": doctor_state,
                "issue_codes": [item["code"] for item in issues],
            },
        )
    return {
        "mode": mode,
        "status": "warning",
        "ready": False,
        "doctor_state": doctor_state,
        "warnings": issues,
        "binding": binding,
    }


def _binding_failure(
    mode: str,
    code: str,
    message: str,
    *,
    warnings: object = None,
) -> dict[str, Any]:
    if mode == "required":
        raise GovernanceError(code, message)
    rows = list(warnings) if isinstance(warnings, list) else []
    rows.append({"code": code, "message": message})
    return {
        "mode": mode,
        "status": "warning",
        "ready": False,
        "valid": False,
        "drift": [],
        "warnings": rows,
        "binding": None,
    }


def _same_path(value: str, expected: Path) -> bool:
    try:
        actual = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return os.path.normcase(str(actual)) == os.path.normcase(str(expected))


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


__all__ = [
    "BINDING_FORMAT",
    "GOVERNANCE_MODES",
    "GovernanceError",
    "inspect_governance",
    "validate_governance_binding",
]
