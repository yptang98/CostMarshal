#!/usr/bin/env python3
"""Validate reviewed, immutable production container build inputs."""

from __future__ import annotations

import argparse
import base64
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "release" / "production-build-inputs.json"
DEFAULT_WORKER_PACKAGE = ROOT / "container" / "worker" / "package.json"
DEFAULT_WORKER_LOCK = ROOT / "container" / "worker" / "package-lock.json"
SCHEMA = "costmarshal-production-build-inputs-v1"
EXPECTED_FIELDS = {
    "schema_version",
    "reviewed_at",
    "review_expires_on",
    "platforms",
    "python_base_image",
    "python_linux_amd64_manifest",
    "python_version",
    "node_base_image",
    "node_linux_amd64_manifest",
    "node_version",
    "codex_npm_version",
    "codex_npm_integrity",
    "codex_npm_lock_sha256",
}
IMAGE = re.compile(r"([a-z0-9][a-z0-9._/-]*:[A-Za-z0-9._-]+)@sha256:([0-9a-f]{64})\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-.][0-9A-Za-z.-]+)?\Z")
INTEGRITY = re.compile(r"sha512-([A-Za-z0-9+/]+={0,2})\Z")


class BuildInputError(ValueError):
    """Reviewed build inputs are missing, stale, mutable, or malformed."""


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _date(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise BuildInputError(f"{label} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BuildInputError(f"{label} must be an ISO date") from exc


def _version(value: Any, label: str) -> str:
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise BuildInputError(f"{label} must be an exact version")
    return value


def _image(value: Any, *, repository: str, label: str) -> str:
    if not isinstance(value, str):
        raise BuildInputError(f"{label} must be a digest-pinned image")
    match = IMAGE.fullmatch(value)
    if not match or not match.group(1).startswith(repository + ":"):
        raise BuildInputError(
            f"{label} must use {repository}:<tag>@sha256:<64 lowercase hex>"
        )
    if ":latest@" in value:
        raise BuildInputError(f"{label} must not use latest")
    return value


def _integrity(value: Any, label: str) -> str:
    match = INTEGRITY.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise BuildInputError(f"{label} must be an npm sha512 value")
    try:
        decoded = base64.b64decode(match.group(1), validate=True)
    except ValueError as exc:
        raise BuildInputError(f"{label} is invalid base64") from exc
    if len(decoded) != 64:
        raise BuildInputError(f"{label} must encode 64 bytes")
    return value


def _json_file(path: Path, label: str) -> tuple[bytes, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BuildInputError(f"{label} is unavailable") from exc
    if len(raw) > 256 * 1024:
        raise BuildInputError(f"{label} exceeds 256 KiB")
    try:
        return raw, json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildInputError(f"{label} is not UTF-8 JSON") from exc


def _validate_worker_lock(
    package_path: Path,
    lock_path: Path,
    *,
    codex_version: str,
    codex_integrity: str,
    expected_lock_sha256: str,
) -> None:
    _, package = _json_file(package_path, "worker package.json")
    lock_raw, lock = _json_file(lock_path, "worker package-lock.json")
    observed_lock_sha256 = "sha256:" + hashlib.sha256(lock_raw).hexdigest()
    if observed_lock_sha256 != expected_lock_sha256:
        raise BuildInputError("worker package-lock.json hash does not match review")
    expected_dependency = {"@openai/codex": codex_version}
    if not isinstance(package, dict) or package.get("dependencies") != expected_dependency:
        raise BuildInputError("worker package.json must declare only the reviewed Codex")
    if (
        not isinstance(lock, dict)
        or lock.get("lockfileVersion") != 3
        or lock.get("requires") is not True
        or not isinstance(lock.get("packages"), dict)
    ):
        raise BuildInputError("worker package-lock.json contract is invalid")
    packages = lock["packages"]
    platform_suffixes = (
        "darwin-arm64",
        "darwin-x64",
        "linux-arm64",
        "linux-x64",
        "win32-arm64",
        "win32-x64",
    )
    expected_entries = {
        "",
        "node_modules/@openai/codex",
        *(f"node_modules/@openai/codex-{suffix}" for suffix in platform_suffixes),
    }
    if set(packages) != expected_entries:
        raise BuildInputError("worker package-lock.json dependency graph is unexpected")
    if packages[""].get("dependencies") != expected_dependency:
        raise BuildInputError("worker lock root dependency does not match package.json")
    installed = packages["node_modules/@openai/codex"]
    if (
        installed.get("version") != codex_version
        or installed.get("integrity") != codex_integrity
        or installed.get("resolved")
        != f"https://registry.npmjs.org/@openai/codex/-/codex-{codex_version}.tgz"
    ):
        raise BuildInputError("worker Codex lock entry does not match review")
    expected_optionals = {
        f"@openai/codex-{suffix}": f"npm:@openai/codex@{codex_version}-{suffix}"
        for suffix in platform_suffixes
    }
    if installed.get("optionalDependencies") != expected_optionals:
        raise BuildInputError("worker Codex optional dependency graph is unexpected")
    for suffix in platform_suffixes:
        entry = packages[f"node_modules/@openai/codex-{suffix}"]
        operating_system, architecture = suffix.rsplit("-", 1)
        version = f"{codex_version}-{suffix}"
        if (
            entry.get("name") != "@openai/codex"
            or entry.get("version") != version
            or entry.get("resolved")
            != f"https://registry.npmjs.org/@openai/codex/-/codex-{version}.tgz"
            or entry.get("optional") is not True
            or entry.get("os") != [operating_system]
            or entry.get("cpu") != [architecture]
        ):
            raise BuildInputError(f"worker Codex {suffix} lock entry is invalid")
        _integrity(entry.get("integrity"), f"worker Codex {suffix} integrity")


def validate(
    path: Path,
    *,
    today: date | None = None,
    worker_package: Path = DEFAULT_WORKER_PACKAGE,
    worker_lock: Path = DEFAULT_WORKER_LOCK,
) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BuildInputError("production build input file is unavailable") from exc
    if len(raw) > 64 * 1024:
        raise BuildInputError("production build input file exceeds 64 KiB")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildInputError("production build input file is not UTF-8 JSON") from exc
    if not isinstance(value, dict) or set(value) != EXPECTED_FIELDS:
        raise BuildInputError("production build inputs have unknown or missing fields")
    if value.get("schema_version") != SCHEMA:
        raise BuildInputError("production build input schema is unsupported")
    reviewed_at = _date(value.get("reviewed_at"), "reviewed_at")
    expires_on = _date(value.get("review_expires_on"), "review_expires_on")
    current = today or date.today()
    if reviewed_at > current:
        raise BuildInputError("production build review date is in the future")
    if expires_on < reviewed_at or expires_on < current:
        raise BuildInputError("production build input review has expired")
    if (expires_on - reviewed_at).days > 31:
        raise BuildInputError("production build input review may cover at most 31 days")
    if value.get("platforms") != ["linux/amd64"]:
        raise BuildInputError("production images must target exactly linux/amd64")
    python_image = _image(
        value.get("python_base_image"),
        repository="python",
        label="python_base_image",
    )
    node_image = _image(
        value.get("node_base_image"),
        repository="node",
        label="node_base_image",
    )
    for field in (
        "python_linux_amd64_manifest",
        "node_linux_amd64_manifest",
    ):
        if not isinstance(value.get(field), str) or not DIGEST.fullmatch(value[field]):
            raise BuildInputError(f"{field} must be sha256:<64 lowercase hex>")
    python_version = _version(value.get("python_version"), "python_version")
    node_version = _version(value.get("node_version"), "node_version")
    codex_version = _version(value.get("codex_npm_version"), "codex_npm_version")
    integrity = _integrity(value.get("codex_npm_integrity"), "codex_npm_integrity")
    lock_sha256 = value.get("codex_npm_lock_sha256")
    if not isinstance(lock_sha256, str) or not DIGEST.fullmatch(lock_sha256):
        raise BuildInputError("codex_npm_lock_sha256 must be sha256:<64 lowercase hex>")
    _validate_worker_lock(
        worker_package,
        worker_lock,
        codex_version=codex_version,
        codex_integrity=integrity,
        expected_lock_sha256=lock_sha256,
    )
    normalized = {
        **value,
        "python_base_image": python_image,
        "node_base_image": node_image,
        "python_version": python_version,
        "node_version": node_version,
        "codex_npm_version": codex_version,
    }
    normalized["build_inputs_sha256"] = (
        "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()
    )
    return normalized


def append_github_output(path: Path, value: dict[str, Any]) -> None:
    fields = (
        "python_base_image",
        "python_linux_amd64_manifest",
        "python_version",
        "node_base_image",
        "node_linux_amd64_manifest",
        "node_version",
        "codex_npm_version",
        "codex_npm_integrity",
        "codex_npm_lock_sha256",
        "build_inputs_sha256",
    )
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{resolved.name}.",
        suffix=".tmp",
        dir=str(resolved.parent),
    )
    temporary = Path(temporary_name)
    try:
        existing = resolved.read_bytes() if resolved.exists() else b""
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(existing)
            for field in fields:
                stream.write(f"{field}={value[field]}\n".encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, resolved)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate committed production image build inputs"
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--github-output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        value = validate(args.input.expanduser().resolve())
        if args.github_output is not None:
            append_github_output(args.github_output, value)
    except BuildInputError as exc:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA,
                    "status": "blocked",
                    "blocker": str(exc),
                },
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "schema_version": SCHEMA,
                "status": "pass",
                "build_inputs_sha256": value["build_inputs_sha256"],
                "reviewed_at": value["reviewed_at"],
                "review_expires_on": value["review_expires_on"],
                "platforms": value["platforms"],
                "python_version": value["python_version"],
                "node_version": value["node_version"],
                "codex_npm_version": value["codex_npm_version"],
                "codex_npm_lock_sha256": value["codex_npm_lock_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
