"""Read-only ArchMarshal governance discovery and binding validation.

This module deliberately has no ArchMarshal import and no launcher discovery.
Callers must provide the exact reviewed canonical ``run_archmarshal.py`` path.
The only supported subprocess operations are the dependency-free bootstrap
identity check and the read-only workspace doctor.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


GOVERNANCE_MODES = frozenset({"off", "auto", "required"})
BINDING_FORMAT = "costmarshal-archmarshal-binding-v3"
ARCHMARSHAL_BOOTSTRAP_API = "archmarshal-plugin-bootstrap-v2"
ARCHMARSHAL_ENGINE_API = "archmarshal-engine-api-v1"
SUPPORTED_ARCHMARSHAL_ENGINE_VERSIONS = frozenset({"0.15.0"})
ARCHMARSHAL_CLI_API = "archmarshal-cli-v1"
ARCHMARSHAL_DOCTOR_API = "archmarshal-doctor-v1"
MAX_LAUNCHER_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_LAUNCHER_BYTES = 2 * 1024 * 1024
MAX_OWNERSHIP_BYTES = 64 * 1024
MAX_SKILL_HEAD_BYTES = 1024
MAX_PROJECT_BYTES = 8 * 1024 * 1024
PROJECT_SNAPSHOT_ATTEMPTS = 3
_BINDING_FIELDS = (
    "format",
    "provider",
    "bootstrap_api_version",
    "engine_api",
    "engine_version",
    "engine_source_sha256",
    "launcher_sha256",
    "launcher_size",
    "invoke_wrapper_sha256",
    "invoke_wrapper_size",
    "doctor_api_version",
    "doctor_payload_schema_version",
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


def load_stable_governance_project(path: Path | str) -> dict[str, Any]:
    """Read the authoritative governance document without mutating runtime state.

    Actor entrypoints cannot rely on the scheduler's earlier check: the binding
    may drift after the spawn effect is emitted.  A bounded double-read rejects
    symlinks/reparse points and any project document changing under inspection.
    """

    project_path = Path(path)
    last_error: GovernanceError | None = None
    for _ in range(PROJECT_SNAPSHOT_ATTEMPTS):
        try:
            first_signature, _ = _capture_project(project_path)
            second_signature, payload = _capture_project(project_path)
        except GovernanceError as exc:
            last_error = exc
            continue
        if first_signature != second_signature:
            last_error = GovernanceError(
                "governance_project_unstable",
                "The CostMarshal governance project document was not stable across reads.",
            )
            continue
        try:
            project = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GovernanceError(
                "governance_project_invalid",
                "The CostMarshal governance project document is invalid.",
            ) from exc
        if not isinstance(project, dict):
            raise GovernanceError(
                "governance_project_invalid",
                "The CostMarshal governance project document is invalid.",
            )
        return project
    raise last_error or GovernanceError(
        "governance_project_unavailable",
        "The CostMarshal governance project document is unavailable.",
    )


def _project_file_state(path: Path) -> tuple[int, int, int, int]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise GovernanceError(
            "governance_project_unavailable",
            "The CostMarshal governance project document is unavailable.",
        ) from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or bool(getattr(info, "st_file_attributes", 0) & reparse_flag)
    ):
        raise GovernanceError(
            "governance_project_unsafe",
            "The CostMarshal governance project document is not a regular file.",
        )
    if info.st_size < 0 or info.st_size > MAX_PROJECT_BYTES:
        raise GovernanceError(
            "governance_project_invalid",
            "The CostMarshal governance project document exceeds the bounded size.",
        )
    return (
        int(info.st_size),
        int(info.st_mtime_ns),
        int(getattr(info, "st_ctime_ns", 0)),
        int(getattr(info, "st_ino", 0)),
    )


def _capture_project(path: Path) -> tuple[tuple[tuple[int, int, int, int], str], bytes]:
    before = _project_file_state(path)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise GovernanceError(
            "governance_project_unavailable",
            "The CostMarshal governance project document could not be read.",
        ) from exc
    after = _project_file_state(path)
    if before != after or len(payload) != before[0]:
        raise GovernanceError(
            "governance_project_unstable",
            "The CostMarshal governance project document changed while it was read.",
        )
    return (before, hashlib.sha256(payload).hexdigest()), payload


def inspect_governance(
    workspace: Path | str,
    *,
    mode: str = "auto",
    launcher_path: Path | str | None = None,
    wrapper_path: Path | str | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Inspect an ArchMarshal workspace without writing to it.

    ``off`` performs no launcher or workspace reads. ``auto`` converts an
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
        launcher, invoke_wrapper = _explicit_launcher_pair(
            launcher_path=launcher_path,
            wrapper_path=wrapper_path,
        )
        launcher_identity = _stable_file_identity(
            launcher,
            label="launcher",
            hash_field="launcher_sha256",
            size_field="launcher_size",
        )
        invoke_identity = _stable_file_identity(
            invoke_wrapper,
            label="invoke wrapper",
            hash_field="invoke_wrapper_sha256",
            size_field="invoke_wrapper_size",
        )
        bootstrap = _invoke_launcher(launcher, ["--bootstrap-status"], timeout_seconds)
        bootstrap_identity = _bootstrap_identity(bootstrap)
        doctor = _invoke_launcher(launcher, ["doctor", str(workspace_path)], timeout_seconds)
        doctor_state, doctor_issues = _doctor_readiness(doctor, workspace_path)
        workspace_identity, identity_issues = _workspace_identity(workspace_path)
        if _stable_file_identity(
            launcher,
            label="launcher",
            hash_field="launcher_sha256",
            size_field="launcher_size",
        ) != launcher_identity or _stable_file_identity(
            invoke_wrapper,
            label="invoke wrapper",
            hash_field="invoke_wrapper_sha256",
            size_field="invoke_wrapper_size",
        ) != invoke_identity:
            raise GovernanceError(
                "archmarshal_launcher_changed",
                "The reviewed ArchMarshal launcher pair changed during inspection.",
            )
        issues = [*doctor_issues, *identity_issues]
        binding = _make_binding(
            bootstrap_identity,
            workspace_path,
            workspace_identity,
            launcher_identity,
            invoke_identity,
        )
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
    launcher_path: Path | str | None = None,
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
    if binding.get("format") != BINDING_FORMAT:
        return _binding_failure(
            normalized_mode,
            "governance_binding_upgrade_required",
            "The stored ArchMarshal binding format requires an explicit CostMarshal rebind.",
        )

    inspection = inspect_governance(
        workspace,
        mode=normalized_mode,
        launcher_path=launcher_path,
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


def _explicit_launcher_pair(
    *,
    launcher_path: Path | str | None,
    wrapper_path: Path | str | None,
) -> tuple[Path, Path]:
    if launcher_path is not None and wrapper_path is not None:
        if os.path.normcase(str(Path(launcher_path).expanduser())) != os.path.normcase(
            str(Path(wrapper_path).expanduser())
        ):
            raise GovernanceError(
                "archmarshal_launcher_ambiguous",
                "Conflicting ArchMarshal launcher paths were provided.",
            )
    selected = launcher_path if launcher_path is not None else wrapper_path
    if selected is None or not str(selected).strip():
        raise GovernanceError(
            "archmarshal_launcher_required",
            "An explicit reviewed canonical ArchMarshal launcher path is required.",
        )
    lexical = Path(selected).expanduser()
    if lexical.name != "run_archmarshal.py":
        raise GovernanceError(
            "archmarshal_canonical_launcher_required",
            "ArchMarshal compatibility requires the canonical run_archmarshal.py launcher.",
        )
    try:
        lexical_info = lexical.lstat()
    except OSError as exc:
        raise GovernanceError(
            "archmarshal_launcher_unavailable",
            "The explicit ArchMarshal launcher path is unavailable.",
        ) from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISREG(lexical_info.st_mode)
        or stat.S_ISLNK(lexical_info.st_mode)
        or bool(getattr(lexical_info, "st_file_attributes", 0) & reparse_flag)
    ):
        raise GovernanceError(
            "archmarshal_launcher_unsafe",
            "The explicit ArchMarshal launcher must be an unlinked regular file.",
        )
    try:
        launcher = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GovernanceError(
            "archmarshal_launcher_unavailable",
            "The explicit ArchMarshal launcher path is unavailable.",
        ) from exc
    invoke_wrapper = launcher.with_name("invoke_archmarshal.py")
    if not invoke_wrapper.exists():
        raise GovernanceError(
            "archmarshal_invoke_wrapper_unavailable",
            "The canonical ArchMarshal invoke wrapper is unavailable beside the launcher.",
        )
    return launcher, invoke_wrapper


def _stable_file_identity(
    path: Path,
    *,
    label: str,
    hash_field: str,
    size_field: str,
) -> dict[str, Any]:
    try:
        before = path.lstat()
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or bool(getattr(before, "st_file_attributes", 0) & reparse_flag)
        ):
            raise GovernanceError(
                "archmarshal_launcher_unsafe",
                f"The reviewed ArchMarshal {label} must be an unlinked regular file.",
            )
        if before.st_size > MAX_LAUNCHER_BYTES:
            raise GovernanceError(
                "archmarshal_launcher_invalid",
                f"The reviewed ArchMarshal {label} exceeds the bounded size.",
            )
        raw = path.read_bytes()
        after = path.lstat()
    except GovernanceError:
        raise
    except OSError as exc:
        raise GovernanceError(
            "archmarshal_launcher_unreadable",
            f"The reviewed ArchMarshal {label} could not be read safely.",
        ) from exc
    if len(raw) > MAX_LAUNCHER_BYTES or (
        before.st_size,
        before.st_mtime_ns,
        getattr(before, "st_ino", None),
    ) != (
        after.st_size,
        after.st_mtime_ns,
        getattr(after, "st_ino", None),
    ):
        raise GovernanceError(
            "archmarshal_launcher_changed",
            f"The reviewed ArchMarshal {label} changed while it was read.",
        )
    return {
        hash_field: hashlib.sha256(raw).hexdigest(),
        size_field: len(raw),
    }


def _invoke_launcher(launcher: Path, arguments: list[str], timeout_seconds: float) -> dict[str, Any]:
    if timeout_seconds <= 0:
        raise GovernanceError(
            "archmarshal_timeout_invalid",
            "ArchMarshal timeout must be positive.",
        )
    try:
        completed = subprocess.run(
            [sys.executable, str(launcher), *arguments],
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
            "archmarshal_launcher_failed",
            "The explicit ArchMarshal launcher could not complete its read-only check.",
        ) from exc
    if completed.returncode != 0:
        raise GovernanceError(
            "archmarshal_launcher_failed",
            "The explicit ArchMarshal launcher rejected its read-only check.",
            details={"returncode": completed.returncode},
        )
    raw = completed.stdout
    if len(raw.encode("utf-8")) > MAX_LAUNCHER_OUTPUT_BYTES:
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
        payload.get("api_version") != ARCHMARSHAL_BOOTSTRAP_API
        or payload.get("verified") is not True
        or payload.get("mode") != "ready"
        or engine_api != ARCHMARSHAL_ENGINE_API
        or engine_version not in SUPPORTED_ARCHMARSHAL_ENGINE_VERSIONS
        or not _is_sha256(source_hash)
    ):
        raise GovernanceError(
            "archmarshal_bootstrap_unverified",
            "ArchMarshal bootstrap identity is missing, mismatched, or unverified.",
        )
    return {
        "bootstrap_api_version": ARCHMARSHAL_BOOTSTRAP_API,
        "engine_api": engine_api,
        "engine_version": engine_version,
        "engine_source_sha256": source_hash,
    }


def _doctor_readiness(
    payload: dict[str, Any], workspace: Path
) -> tuple[str, list[dict[str, str]]]:
    state = payload.get("state")
    issues: list[dict[str, str]] = []
    if payload.get("api_version") != ARCHMARSHAL_CLI_API:
        issues.append(
            {
                "code": "archmarshal_doctor_api_invalid",
                "message": "ArchMarshal doctor returned an unsupported API envelope.",
            }
        )
    if payload.get("payload_schema_version") != ARCHMARSHAL_DOCTOR_API:
        issues.append(
            {
                "code": "archmarshal_doctor_schema_invalid",
                "message": "ArchMarshal doctor returned an unsupported payload schema.",
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
    summary_valid = (
        isinstance(summary, dict)
        and set(summary) == {"error", "warning", "info"}
        and all(type(summary.get(key)) is int and summary[key] >= 0 for key in summary)
    )
    if not summary_valid:
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
    elif state == "healthy" and summary["warning"] != 0:
        issues.append(
            {
                "code": "archmarshal_doctor_warnings",
                "message": "ArchMarshal doctor reported warnings for a healthy workspace.",
            }
        )
    findings = payload.get("findings")
    if not isinstance(findings, list) or any(not isinstance(item, dict) for item in findings):
        issues.append(
            {
                "code": "archmarshal_doctor_findings_invalid",
                "message": "ArchMarshal doctor findings must be a bounded list of objects.",
            }
        )
        findings = []
    blocking = sorted(
        {
            str(item.get("classification"))
            for item in findings
            if item.get("classification")
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
    if not os.path.lexists(path):
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
    if not os.path.lexists(path):
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
    launcher_identity: dict[str, Any],
    invoke_identity: dict[str, Any],
) -> dict[str, Any]:
    return {
        "format": BINDING_FORMAT,
        "provider": "archmarshal",
        **bootstrap,
        **launcher_identity,
        **invoke_identity,
        "doctor_api_version": ARCHMARSHAL_CLI_API,
        "doctor_payload_schema_version": ARCHMARSHAL_DOCTOR_API,
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


def governance_launcher_path(governance: dict[str, Any]) -> Path | str | None:
    """Return the configured canonical launcher, with a legacy field fallback."""

    launcher = governance.get("launcher_path")
    if launcher is not None and str(launcher).strip():
        return launcher
    wrapper = governance.get("wrapper_path")
    if wrapper is not None and str(wrapper).strip():
        return wrapper
    return None


def enforce_governance_contract(
    governance: dict[str, Any],
    workspace: Path | str,
    *,
    operation: str,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Fail closed before provider side effects, including auto rediscovery.

    CostMarshal never adopts or repairs an ArchMarshal workspace. If an auto
    project transitions from explicitly absent to managed, an explicit rebind
    is required before execution can continue.
    """

    if not isinstance(governance, dict):
        raise GovernanceError(
            "governance_state_invalid",
            f"ArchMarshal governance state is invalid for {operation}.",
        )
    mode = _governance_mode(str(governance.get("mode") or "off"))
    ready = governance.get("ready", False)
    if type(ready) is not bool:
        raise GovernanceError(
            "governance_state_invalid",
            f"ArchMarshal governance readiness is invalid for {operation}.",
        )
    if mode == "off":
        return {"mode": "off", "governed": False, "ready": False, "validation": None}

    launcher = governance_launcher_path(governance)
    if mode == "required" or ready:
        validation = validate_governance_binding(
            governance.get("binding"),
            workspace,
            mode="required",
            launcher_path=launcher,
            timeout_seconds=timeout_seconds,
        )
        if not validation.get("valid"):
            raise GovernanceError(
                "governance_binding_invalid",
                f"ArchMarshal governance binding is invalid for {operation}.",
            )
        return {"mode": mode, "governed": True, "ready": True, "validation": validation}

    workspace_path = _workspace_path(workspace)
    marker_path = workspace_path / ".agent" / "ownership.json"
    marker_hash, marker, marker_issue = _ownership_marker(marker_path)
    marker_absent = (
        marker_hash is None
        and marker is None
        and isinstance(marker_issue, dict)
        and marker_issue.get("code") == "archmarshal_ownership_absent"
    )
    if launcher is None:
        if not marker_absent:
            raise GovernanceError(
                "archmarshal_launcher_required_for_detected_governance",
                "ArchMarshal governance was detected after CostMarshal initialization; configure the canonical launcher and rebind.",
            )
        return {"mode": mode, "governed": False, "ready": False, "validation": None}

    inspection = inspect_governance(
        workspace_path,
        mode="auto",
        launcher_path=launcher,
        timeout_seconds=timeout_seconds,
    )
    binding = inspection.get("binding")
    warning_codes = {
        str(item.get("code"))
        for item in inspection.get("warnings") or []
        if isinstance(item, dict)
    }
    explicitly_absent = (
        inspection.get("doctor_state") == "absent"
        and isinstance(binding, dict)
        and binding.get("ownership_marker_sha256") is None
        and warning_codes.issubset(
            {"archmarshal_workspace_not_ready", "archmarshal_ownership_absent"}
        )
    )
    if explicitly_absent:
        return {
            "mode": mode,
            "governed": False,
            "ready": False,
            "validation": inspection,
        }
    if inspection.get("ready") or (
        isinstance(binding, dict) and binding.get("ownership_marker_sha256") is not None
    ):
        raise GovernanceError(
            "governance_rebind_required",
            "ArchMarshal governance changed after CostMarshal initialization; an explicit CostMarshal rebind is required.",
        )
    raise GovernanceError(
        "governance_inspection_unavailable",
        "ArchMarshal governance could not be proven absent before provider execution.",
        details={"warning_codes": sorted(warning_codes)},
    )


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
    "ARCHMARSHAL_BOOTSTRAP_API",
    "ARCHMARSHAL_CLI_API",
    "ARCHMARSHAL_DOCTOR_API",
    "ARCHMARSHAL_ENGINE_API",
    "BINDING_FORMAT",
    "GOVERNANCE_MODES",
    "GovernanceError",
    "SUPPORTED_ARCHMARSHAL_ENGINE_VERSIONS",
    "enforce_governance_contract",
    "governance_launcher_path",
    "inspect_governance",
    "load_stable_governance_project",
    "validate_governance_binding",
]
