"""Security boundary helpers for CostMarshal v2."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path, PureWindowsPath


class SecurityValidationError(ValueError):
    """Raised when untrusted control-plane input is unsafe or ambiguous."""


_TASK_ID_RE = re.compile(r"V2-[0-9]{4,12}\Z")
_ACTOR_ID_RE = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*\Z")
_PROJECT_ID_RE = re.compile(r"[0-9]{8}-[0-9]{6}-[a-z0-9]+(?:[-_.][a-z0-9]+)*\Z")
_ENV_KEY_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,127}\Z")

DEFAULT_RESERVED_NAMES = frozenset({
    ".agent",
    ".agents",
    "agents.md",
    "agents.override.md",
    ".git",
    ".codex",
    ".codex-plugin",
})
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)


def _require_text(value: object, label: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise SecurityValidationError(f"{label} must be a string")
    if not value or value != value.strip():
        raise SecurityValidationError(f"{label} must be non-empty and have no surrounding whitespace")
    if "\x00" in value:
        raise SecurityValidationError(f"{label} must not contain NUL")
    if len(value) > max_length:
        raise SecurityValidationError(f"{label} exceeds {max_length} characters")
    return value


def validate_task_id(value: object) -> str:
    """Return a canonical task id or reject it.

    Task ids are deliberately narrower than filesystem-safe slugs so they
    cannot be confused with actor or project identifiers.
    """

    text = _require_text(value, "task id", max_length=15)
    if not _TASK_ID_RE.fullmatch(text):
        raise SecurityValidationError("task id must match V2-[0-9]{4,12}")
    return text


def validate_actor_id(value: object) -> str:
    """Return a canonical lowercase actor id or reject it."""

    text = _require_text(value, "actor id", max_length=72)
    if not _ACTOR_ID_RE.fullmatch(text):
        raise SecurityValidationError(
            "actor id must be lowercase ASCII segments separated by one '-', '_', or '.'"
        )
    if text.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_NAMES:
        raise SecurityValidationError("actor id must not be a Windows device name")
    return text


def validate_project_id(value: object) -> str:
    """Validate the timestamp-prefixed id emitted by ``make_project_id``."""

    text = _require_text(value, "project id", max_length=88)
    if not _PROJECT_ID_RE.fullmatch(text):
        raise SecurityValidationError(
            "project id must match YYYYMMDD-HHMMSS-<lowercase-slug>"
        )
    try:
        datetime.strptime(text[:15], "%Y%m%d-%H%M%S")
    except ValueError as exc:
        raise SecurityValidationError("project id contains an invalid calendar timestamp") from exc
    return text


def validate_env_key(value: object) -> str:
    """Validate a provider's environment-variable name."""

    text = _require_text(value, "env key", max_length=128)
    if not _ENV_KEY_RE.fullmatch(text):
        raise SecurityValidationError("env key must be uppercase ASCII shell identifier")
    return text


def normalize_scoped_path(value: object, *, label: str = "path") -> str:
    """Normalize a workspace-relative claim/allow path.

    The same rules are applied on every host.  Windows drive-relative paths,
    UNC/device paths, alternate data streams, traversal, dot segments, and NUL
    are rejected even when validation runs on POSIX.
    """

    text = _require_text(value, label, max_length=4096)
    windows = PureWindowsPath(text)
    if text.startswith(("/", "\\")) or windows.drive or windows.root or windows.is_absolute():
        raise SecurityValidationError(f"{label} must be workspace-relative")
    if ":" in text:
        raise SecurityValidationError(f"{label} must not contain ':' (drive/ADS syntax)")
    normalized = text.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if not parts:
        raise SecurityValidationError(f"{label} must not be empty")
    if any(part in {".", ".."} for part in parts):
        raise SecurityValidationError(f"{label} must not contain '.' or '..' segments")
    if any("\x00" in part for part in parts):
        raise SecurityValidationError(f"{label} must not contain NUL")
    for part in parts:
        if part[-1] in {" ", "."}:
            raise SecurityValidationError(f"{label} components must not end in space or '.'")
        if part.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_NAMES:
            raise SecurityValidationError(f"{label} contains a Windows device name: {part}")
    return "/".join(parts)


def is_reserved_path(value: object, *, reserved_names: Iterable[str] = DEFAULT_RESERVED_NAMES) -> bool:
    """Return whether any component belongs to a protected governance path."""

    normalized = normalize_scoped_path(value)
    reserved = {str(name).casefold() for name in reserved_names}
    return any(part.casefold() in reserved for part in normalized.split("/"))


def normalize_claim_path(
    value: object,
    *,
    allow_reserved: bool = False,
    reserved_names: Iterable[str] = DEFAULT_RESERVED_NAMES,
) -> str:
    """Normalize one claimed write path, protecting governance paths by default."""

    normalized = normalize_scoped_path(value, label="claim path")
    if not allow_reserved and is_reserved_path(normalized, reserved_names=reserved_names):
        raise SecurityValidationError(f"claim path is reserved: {normalized}")
    return normalized


def normalize_allowed_path(
    value: object,
    *,
    allow_reserved: bool = False,
    reserved_names: Iterable[str] = DEFAULT_RESERVED_NAMES,
) -> str:
    """Normalize one allowed context/write path with the same safe defaults."""

    normalized = normalize_scoped_path(value, label="allowed path")
    if not allow_reserved and is_reserved_path(normalized, reserved_names=reserved_names):
        raise SecurityValidationError(f"allowed path is reserved: {normalized}")
    return normalized


def normalize_path_list(
    values: Iterable[object],
    *,
    kind: str = "claim",
    allow_reserved: bool = False,
) -> tuple[str, ...]:
    """Normalize and case-insensitively deduplicate claim or allowed paths."""

    normalizer = normalize_claim_path if kind == "claim" else normalize_allowed_path
    if kind not in {"claim", "allowed"}:
        raise SecurityValidationError("path list kind must be 'claim' or 'allowed'")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalizer(value, allow_reserved=allow_reserved)
        key = normalized.casefold()
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return tuple(result)


def ensure_workspace_containment(
    workspace: os.PathLike[str] | str,
    candidate: os.PathLike[str] | str,
    *,
    must_exist: bool = False,
) -> Path:
    """Resolve ``candidate`` and prove it remains under ``workspace``.

    Existing symlinks are resolved by :meth:`Path.resolve`; containment is
    checked with ``commonpath`` to handle component boundaries and drive
    mismatches correctly.
    """

    root = Path(workspace).expanduser().resolve(strict=True)
    candidate_text = os.fspath(candidate)
    windows_candidate = PureWindowsPath(candidate_text)
    if os.name != "nt" and (windows_candidate.drive or candidate_text.startswith("\\")):
        raise SecurityValidationError("candidate uses an absolute Windows path outside this workspace")
    raw_candidate = Path(candidate_text).expanduser()
    joined = raw_candidate if raw_candidate.is_absolute() else root / raw_candidate
    resolved = joined.resolve(strict=must_exist)
    try:
        common = Path(os.path.commonpath((str(root), str(resolved))))
    except ValueError as exc:
        raise SecurityValidationError("candidate is on a different workspace root") from exc
    if os.path.normcase(str(common)) != os.path.normcase(str(root)):
        raise SecurityValidationError(f"candidate escapes workspace: {candidate}")
    return resolved


def resolve_workspace_path(
    workspace: os.PathLike[str] | str,
    relative_path: object,
    *,
    allow_reserved: bool = False,
    must_exist: bool = False,
) -> Path:
    """Validate a scoped path and resolve it beneath a workspace."""

    normalized = normalize_allowed_path(relative_path, allow_reserved=allow_reserved)
    return ensure_workspace_containment(workspace, normalized, must_exist=must_exist)


def parse_secrets_text(text: object) -> dict[str, str]:
    """Parse strict dotenv-style text without reading or mutating process state."""

    if not isinstance(text, str):
        raise SecurityValidationError("secrets text must be a string")
    if "\x00" in text:
        raise SecurityValidationError("secrets text must not contain NUL")
    secrets: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.lstrip("\ufeff").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise SecurityValidationError(f"invalid secrets line {line_number}: expected KEY=VALUE")
        raw_key, raw_value = line.split("=", 1)
        key = validate_env_key(raw_key.strip())
        if key in secrets:
            raise SecurityValidationError(f"duplicate secrets key on line {line_number}: {key}")
        value = raw_value.strip()
        if value[:1] in {'"', "'"}:
            if len(value) < 2 or value[-1] != value[0]:
                raise SecurityValidationError(f"unterminated quoted secret on line {line_number}")
            value = value[1:-1]
        if "\r" in value or "\n" in value or "\x00" in value:
            raise SecurityValidationError(f"secret value on line {line_number} contains a control character")
        secrets[key] = value
    return secrets


def provider_env_from_secrets(
    secrets: Mapping[str, str],
    env_key: object,
) -> dict[str, str]:
    """Return exactly one provider credential, never the complete secret set."""

    key = validate_env_key(env_key)
    if key not in secrets:
        raise SecurityValidationError(f"provider secret is missing: {key}")
    value = secrets[key]
    if not isinstance(value, str) or not value or "\x00" in value or "\r" in value or "\n" in value:
        raise SecurityValidationError(f"provider secret is empty or invalid: {key}")
    return {key: value}


def provider_env_from_secrets_text(text: object, env_key: object) -> dict[str, str]:
    """Pure composition of strict parsing and one-key provider selection."""

    return provider_env_from_secrets(parse_secrets_text(text), env_key)


def load_provider_env(secrets_file: os.PathLike[str] | str, env_key: object) -> dict[str, str]:
    """Read a secrets file and return only the requested provider credential."""

    try:
        path = Path(secrets_file).expanduser().resolve(strict=True)
    except OSError as exc:
        raise SecurityValidationError(f"secrets file cannot be resolved: {secrets_file}") from exc
    if not path.is_file():
        raise SecurityValidationError(f"secrets path is not a file: {path}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SecurityValidationError(f"secrets file cannot be read: {path}") from exc
    return provider_env_from_secrets_text(text, env_key)


def redact_secret_values(
    text: object,
    secrets: Mapping[str, str] | Iterable[str],
    *,
    replacement: str = "[REDACTED]",
) -> str:
    """Redact the exact secret values known for the current provider/process."""

    if not isinstance(text, str):
        raise SecurityValidationError("redaction input must be a string")
    if isinstance(secrets, Mapping):
        values: Iterable[str] = secrets.values()
    elif isinstance(secrets, str):
        values = (secrets,)
    else:
        values = secrets
    unique = {value for value in values if isinstance(value, str) and value}
    result = text
    for value in sorted(unique, key=len, reverse=True):
        result = result.replace(value, replacement)
    return result
