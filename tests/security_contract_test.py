#!/usr/bin/env python3
"""Cross-platform contract tests for CostMarshal security primitives."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from costmarshal_v2.security import (  # noqa: E402
    SecurityValidationError,
    ensure_workspace_containment,
    is_reserved_path,
    load_provider_env,
    normalize_allowed_path,
    normalize_claim_path,
    normalize_path_list,
    normalize_scoped_path,
    parse_secrets_text,
    provider_env_from_secrets,
    provider_env_from_secrets_text,
    redact_secret_values,
    resolve_workspace_path,
    validate_actor_id,
    validate_env_key,
    validate_project_id,
    validate_task_id,
)


def rejects(function, *args, **kwargs) -> None:
    try:
        function(*args, **kwargs)
    except SecurityValidationError:
        return
    raise AssertionError(f"expected SecurityValidationError: {function.__name__}{args!r}")


def test_identifiers() -> None:
    assert validate_task_id("V2-0001") == "V2-0001"
    assert validate_task_id("V2-123456789012") == "V2-123456789012"
    for value in ("", " V2-0001", "v2-0001", "V2-1", "V2-0001/../x", "V2-0001:x", None):
        rejects(validate_task_id, value)

    for value in ("leader", "scheduler", "agent-v2-0001", "agent_v2.0001"):
        assert validate_actor_id(value) == value
    for value in ("Leader", ".leader", "leader.", "agent--one", "agent/one", "agent:one", "con", "NUL", "a" * 73):
        rejects(validate_actor_id, value)

    project_id = "20260715-235959-costmarshal-v2"
    assert validate_project_id(project_id) == project_id
    for value in ("costmarshal", "2026715-235959-x", "20260230-235959-x", "20260715-246000-x", "20260715-235959-X", "20260715-235959-a..b", "../20260715-235959-x"):
        rejects(validate_project_id, value)

    assert validate_env_key("LONGCAT_API_KEY") == "LONGCAT_API_KEY"
    for value in ("longcat_key", "1KEY", "KEY-NAME", "KEY=VALUE", ""):
        rejects(validate_env_key, value)


def test_paths() -> None:
    assert normalize_scoped_path("src\\module//file.py/") == "src/module/file.py"
    assert normalize_claim_path("reports/result.md") == "reports/result.md"
    assert normalize_allowed_path("src/package") == "src/package"
    assert normalize_path_list(["src/A.py", "src/A.py", "docs/readme.md"], kind="claim") == (
        "src/A.py",
        "docs/readme.md",
    )
    assert normalize_path_list(["src/A.py", "SRC/a.py"], kind="allowed") == ("src/A.py",)
    rejects(normalize_path_list, ["src"], kind="unknown")

    unsafe = (
        "",
        "   ",
        ".",
        "./src",
        "src/./file",
        "../src",
        "src/../file",
        "/etc/passwd",
        "\\rooted",
        "C:\\Windows\\system.ini",
        "C:relative.txt",
        "\\\\server\\share\\file",
        "\\\\?\\C:\\Windows",
        "file.txt:secret-stream",
        "src/file. ",
        "src/file.",
        "src/CON.txt",
        "LPT9/output.txt",
        "https://example.test/file",
        "bad\x00name",
    )
    for value in unsafe:
        rejects(normalize_scoped_path, value)

    reserved = (
        ".agent/state.json",
        "nested/.agents/skills/x",
        "AGENTS.md",
        "docs/agents.MD",
        ".git/config",
        "nested/.GIT/index",
        ".codex-plugin/plugin.json",
    )
    for value in reserved:
        assert is_reserved_path(value)
        rejects(normalize_claim_path, value)
        rejects(normalize_allowed_path, value)
        assert normalize_claim_path(value, allow_reserved=True) == normalize_scoped_path(value)
    assert not is_reserved_path("src/agent/state.json")


def test_workspace_containment() -> None:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-security-contract-"))
    try:
        workspace = temp / "workspace"
        workspace.mkdir()
        existing = workspace / "src" / "file.txt"
        existing.parent.mkdir()
        existing.write_text("ok\n", encoding="utf-8")
        assert resolve_workspace_path(workspace, "src/file.txt", must_exist=True) == existing.resolve()
        assert ensure_workspace_containment(workspace, existing, must_exist=True) == existing.resolve()
        rejects(ensure_workspace_containment, workspace, temp / "outside.txt")
        if os.name != "nt":
            assert ensure_workspace_containment(
                workspace,
                existing.resolve(),
                must_exist=True,
            ) == existing.resolve()
            rejects(ensure_workspace_containment, workspace, r"C:\\outside.txt")
            rejects(ensure_workspace_containment, workspace, r"\outside.txt")
            rejects(ensure_workspace_containment, workspace, r"\\server\share\outside.txt")
        rejects(resolve_workspace_path, workspace, "../outside.txt")
        rejects(resolve_workspace_path, workspace, ".git/config")

        outside = temp / "outside"
        outside.mkdir()
        link = workspace / "escape-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pass
        else:
            rejects(resolve_workspace_path, workspace, "escape-link/new.txt")
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def test_provider_secrets_and_redaction() -> None:
    text = "\ufeff# provider credentials\nLONGCAT_API_KEY='lc-secret-value'\nOPENAI_API_KEY=oa-secret-value\nexport MID_API_KEY=mid-secret\n"
    parsed = parse_secrets_text(text)
    assert parsed == {
        "LONGCAT_API_KEY": "lc-secret-value",
        "OPENAI_API_KEY": "oa-secret-value",
        "MID_API_KEY": "mid-secret",
    }
    selected = provider_env_from_secrets(parsed, "MID_API_KEY")
    assert selected == {"MID_API_KEY": "mid-secret"}
    assert provider_env_from_secrets_text(text, "LONGCAT_API_KEY") == {
        "LONGCAT_API_KEY": "lc-secret-value"
    }
    assert os.environ.get("MID_API_KEY") != "mid-secret", "pure selector must not mutate os.environ"
    rejects(provider_env_from_secrets, parsed, "MISSING_API_KEY")
    rejects(provider_env_from_secrets, {"EMPTY_API_KEY": ""}, "EMPTY_API_KEY")
    for invalid in ("BROKEN", "DUP=x\nDUP=y\n", "bad=x\n", "KEY='unterminated\n", "KEY=a\x00b\n"):
        rejects(parse_secrets_text, invalid)

    temp = Path(tempfile.mkdtemp(prefix="costmarshal-secret-contract-"))
    try:
        secret_file = temp / "providers.env"
        secret_file.write_text(text, encoding="utf-8")
        assert load_provider_env(secret_file, "OPENAI_API_KEY") == {
            "OPENAI_API_KEY": "oa-secret-value"
        }
        rejects(load_provider_env, temp / "missing.env", "OPENAI_API_KEY")
    finally:
        shutil.rmtree(temp, ignore_errors=True)

    message = "Bearer lc-secret-value; duplicate lc-secret-value; other oa-secret-value; prefix abc123."
    redacted = redact_secret_values(message, parsed)
    assert redacted == "Bearer [REDACTED]; duplicate [REDACTED]; other [REDACTED]; prefix abc123."
    assert not any(value in redacted for value in parsed.values())
    assert redact_secret_values("token-abcdef", ["abc", "abcdef"]) == "token-[REDACTED]"
    assert redact_secret_values("token-abcdef", "abcdef") == "token-[REDACTED]"
    assert redact_secret_values("unchanged", []) == "unchanged"
    rejects(redact_secret_values, None, parsed)


def main() -> int:
    test_identifiers()
    test_paths()
    test_workspace_containment()
    test_provider_secrets_and_redaction()
    print(json.dumps({"status": "ok", "platform": os.name, "contracts": 4}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
