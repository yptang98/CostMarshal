from __future__ import annotations

"""Immutable, hash-bound provider profile snapshots.

The scheduler binds the exact UTF-8 TOML bytes admitted by a route. Runners
consume only the durable snapshot and never re-resolve a mutable CODEX_HOME.
"""

import hashlib
import contextlib
import os
import re
import stat
import tempfile
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


PROFILE_BINDING_SCHEMA = "costmarshal-profile-binding-v1"
MAX_PROFILE_BYTES = 256 * 1024
_SAFE_PROFILE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9])?\Z")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ProfileBindingError(ValueError):
    pass


def synthetic_default_profile(*, snapshot_relpath: str) -> tuple[bytes, dict[str, Any]]:
    payload = b"# CostMarshal isolated default profile\n"
    return payload, _binding(
        payload,
        logical_name=None,
        source_kind="synthetic-default",
        provider_identity=None,
        base_url=None,
        wire_api=None,
        env_key=None,
        model=None,
        snapshot_relpath=snapshot_relpath,
    )


def unavailable_profile_binding(
    profile: str,
    *,
    env_key: str | None,
    snapshot_relpath: str,
) -> dict[str, Any]:
    _validate_profile_name(profile)
    return {
        "schema_version": PROFILE_BINDING_SCHEMA,
        "status": "unavailable",
        "logical_name": profile,
        "source_kind": "named-profile",
        "sha256": None,
        "size_bytes": None,
        "provider_identity": None,
        "base_url": None,
        "wire_api": None,
        "env_key": env_key,
        "model": None,
        "snapshot_relpath": _validate_snapshot_relpath(snapshot_relpath),
    }


def read_named_profile(
    profile: str,
    *,
    expected_env_key: str | None,
    snapshot_relpath: str,
    codex_home: str | os.PathLike[str] | None = None,
) -> tuple[bytes, dict[str, Any]] | None:
    """Read one stable regular file and return its exact bytes plus identity."""

    _validate_profile_name(profile)
    raw_home = codex_home if codex_home is not None else os.environ.get("CODEX_HOME")
    if not raw_home:
        return None
    home = Path(raw_home).expanduser().resolve()
    source = home / f"{profile}.config.toml"
    try:
        before = source.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProfileBindingError(f"provider profile cannot be inspected: {profile}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ProfileBindingError(f"provider profile must be a non-link regular file: {profile}")
    try:
        if source.resolve(strict=True).parent != home:
            raise ProfileBindingError(f"provider profile escapes CODEX_HOME: {profile}")
    except OSError as exc:
        raise ProfileBindingError(f"provider profile cannot be resolved: {profile}") from exc
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise ProfileBindingError(f"provider profile cannot be opened safely: {profile}") from exc
    try:
        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode):
            raise ProfileBindingError(f"provider profile must be a regular file: {profile}")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, MAX_PROFILE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_PROFILE_BYTES:
                raise ProfileBindingError("provider profile exceeds 256 KiB")
        opened_after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after = source.lstat()
    except OSError as exc:
        raise ProfileBindingError(f"provider profile changed while being read: {profile}") from exc
    identity_before = _stat_identity(before)
    if (
        identity_before != _stat_identity(opened_before)
        or _stat_identity(opened_before) != _stat_identity(opened_after)
        or _stat_identity(opened_after) != _stat_identity(after)
    ):
        raise ProfileBindingError(f"provider profile changed while being read: {profile}")
    payload = b"".join(chunks)
    parsed = parse_profile_bytes(payload)
    allowed_top = {
        "model_provider",
        "model",
        "model_reasoning_effort",
        "disable_response_storage",
        "web_search",
        "model_providers",
    }
    if set(parsed) - allowed_top:
        raise ProfileBindingError("provider profile contains unsupported settings")
    provider_identity = parsed.get("model_provider")
    providers = parsed.get("model_providers")
    if not isinstance(provider_identity, str) or not isinstance(providers, dict):
        raise ProfileBindingError("provider profile must declare model_provider and model_providers")
    row = providers.get(provider_identity)
    if not isinstance(row, dict):
        raise ProfileBindingError("provider profile selected model_provider is missing")
    if set(row) - {"name", "base_url", "wire_api", "env_key"}:
        raise ProfileBindingError("provider profile contains unsupported provider settings")
    env_key = row.get("env_key")
    if env_key != expected_env_key:
        raise ProfileBindingError("provider profile env_key does not match the routed provider")
    base_url = str(row.get("base_url") or "")
    parsed_url = urlsplit(base_url)
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.netloc
        or parsed_url.username
        or parsed_url.password
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise ProfileBindingError("provider profile base_url is invalid")
    return payload, _binding(
        payload,
        logical_name=profile,
        source_kind="named-profile",
        provider_identity=provider_identity,
        base_url=base_url,
        wire_api=row.get("wire_api"),
        env_key=env_key,
        model=parsed.get("model"),
        snapshot_relpath=snapshot_relpath,
    )


def parse_profile_bytes(payload: bytes) -> dict[str, Any]:
    if len(payload) > MAX_PROFILE_BYTES:
        raise ProfileBindingError("provider profile exceeds 256 KiB")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProfileBindingError("provider profile must be UTF-8") from exc
    if "\x00" in text:
        raise ProfileBindingError("provider profile contains a NUL byte")
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileBindingError("provider profile is invalid TOML") from exc
    if not isinstance(parsed, dict):
        raise ProfileBindingError("provider profile is invalid TOML")
    return parsed


def validate_profile_binding(binding: Any, *, require_available: bool = False) -> dict[str, Any]:
    if not isinstance(binding, dict):
        raise ProfileBindingError("profile binding must be an object")
    expected = {
        "schema_version",
        "status",
        "logical_name",
        "source_kind",
        "sha256",
        "size_bytes",
        "provider_identity",
        "base_url",
        "wire_api",
        "env_key",
        "model",
        "snapshot_relpath",
    }
    if set(binding) != expected or binding.get("schema_version") != PROFILE_BINDING_SCHEMA:
        raise ProfileBindingError("profile binding schema is invalid")
    status_value = binding.get("status")
    if status_value not in {"available", "unavailable"}:
        raise ProfileBindingError("profile binding status is invalid")
    if require_available and status_value != "available":
        raise ProfileBindingError("admitted provider profile was unavailable; replan explicitly")
    logical_name = binding.get("logical_name")
    source_kind = binding.get("source_kind")
    if source_kind == "named-profile":
        if not isinstance(logical_name, str):
            raise ProfileBindingError("named profile binding lacks a logical name")
        _validate_profile_name(logical_name)
    elif source_kind == "synthetic-default":
        if logical_name is not None:
            raise ProfileBindingError("synthetic profile binding has a logical name")
    else:
        raise ProfileBindingError("profile binding source kind is invalid")
    _validate_snapshot_relpath(binding.get("snapshot_relpath"))
    if status_value == "available":
        if not isinstance(binding.get("sha256"), str) or not _SHA256.fullmatch(binding["sha256"]):
            raise ProfileBindingError("profile binding sha256 is invalid")
        if type(binding.get("size_bytes")) is not int or not 0 <= binding["size_bytes"] <= MAX_PROFILE_BYTES:
            raise ProfileBindingError("profile binding size is invalid")
    elif binding.get("sha256") is not None or binding.get("size_bytes") is not None:
        raise ProfileBindingError("unavailable profile binding contains a content identity")
    return binding


def snapshot_path(root: Path, binding: dict[str, Any]) -> Path:
    validate_profile_binding(binding)
    resolved_root = root.resolve()
    target = (resolved_root / binding["snapshot_relpath"]).resolve()
    try:
        target.relative_to(resolved_root)
    except ValueError as exc:
        raise ProfileBindingError("profile snapshot escapes the runtime root") from exc
    return target


def install_profile_snapshot(root: Path, payload: bytes, binding: dict[str, Any]) -> Path:
    validate_profile_binding(binding, require_available=True)
    _verify_payload(payload, binding)
    target = snapshot_path(root, binding)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        verify_profile_snapshot(root, binding)
        return target
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        with os.fdopen(os.open(temporary, os.O_RDONLY | getattr(os, "O_BINARY", 0)), "rb") as handle:
            written = handle.read(MAX_PROFILE_BYTES + 1)
        _verify_payload(written, binding)
        with contextlib.suppress(OSError):
            os.chmod(temporary, 0o400)
        os.replace(temporary, target)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
    verify_profile_snapshot(root, binding)
    return target


def verify_profile_snapshot(root: Path, binding: dict[str, Any]) -> bytes:
    validate_profile_binding(binding, require_available=True)
    target = snapshot_path(root, binding)
    try:
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ProfileBindingError("profile snapshot is not a non-link regular file")
        payload = target.read_bytes()
    except OSError as exc:
        raise ProfileBindingError("profile snapshot is missing or unreadable") from exc
    _verify_payload(payload, binding)
    return payload


def install_bound_copy(destination: Path, payload: bytes, binding: dict[str, Any]) -> None:
    validate_profile_binding(binding, require_available=True)
    _verify_payload(payload, binding)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        try:
            existing = destination.read_bytes()
        except OSError as exc:
            raise ProfileBindingError("bound profile destination is unreadable") from exc
        if existing == payload:
            return
        raise ProfileBindingError("bound profile destination conflicts with the admitted snapshot")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=str(destination.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def _binding(
    payload: bytes,
    *,
    logical_name: str | None,
    source_kind: str,
    provider_identity: str | None,
    base_url: str | None,
    wire_api: Any,
    env_key: Any,
    model: Any,
    snapshot_relpath: str,
) -> dict[str, Any]:
    return {
        "schema_version": PROFILE_BINDING_SCHEMA,
        "status": "available",
        "logical_name": logical_name,
        "source_kind": source_kind,
        "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "provider_identity": provider_identity,
        "base_url": base_url,
        "wire_api": wire_api,
        "env_key": env_key,
        "model": model,
        "snapshot_relpath": _validate_snapshot_relpath(snapshot_relpath),
    }


def _verify_payload(payload: bytes, binding: dict[str, Any]) -> None:
    if len(payload) != binding.get("size_bytes"):
        raise ProfileBindingError("profile snapshot size does not match its binding")
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    if digest != binding.get("sha256"):
        raise ProfileBindingError("profile snapshot hash does not match its binding")


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(getattr(info, "st_ctime_ns", 0)),
    )


def _validate_profile_name(profile: str) -> str:
    if not isinstance(profile, str) or not _SAFE_PROFILE.fullmatch(profile) or profile in {".", ".."}:
        raise ProfileBindingError("provider profile name must be a safe identifier")
    return profile


def _validate_snapshot_relpath(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ProfileBindingError("profile snapshot path is invalid")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ProfileBindingError("profile snapshot path is invalid")
    return path.as_posix()

