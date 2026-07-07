#!/usr/bin/env python3
"""Install/uninstall smoke test for the CostMarshal skill.

This simulates the documented install prompt in a temporary CODEX_HOME.
It does not touch the user's real Codex home or runtime state.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1]


IGNORED_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "costmarshal",
    "projects",
    "memory",
    "config",
}


def ignore_install_artifacts(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        path = Path(directory) / name
        if name in IGNORED_DIRS:
            ignored.add(name)
        elif name.endswith((".pyc", ".pyo", ".env")):
            ignored.add(name)
        elif path.is_file() and name.lower() in {".env", "secrets.json"}:
            ignored.add(name)
    return ignored


def run(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)
    if result.returncode != 0:
        raise AssertionError(
            f"Command failed: {' '.join(command)}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-install-smoke-"))
    try:
        codex_home = temp / "codex-home"
        skills_dir = codex_home / "skills"
        install_dir = skills_dir / "costmarshal"
        shutil.copytree(SOURCE, install_dir, ignore=ignore_install_artifacts)

        assert_true((install_dir / "SKILL.md").is_file(), "installed skill should include SKILL.md")
        assert_true((install_dir / "scripts" / "costmarshal.py").is_file(), "installed skill should include CLI")
        assert_true(not (install_dir / ".git").exists(), "install copy should not include .git")
        assert_true(not any(install_dir.rglob("*.env")), "install copy should not include .env files")

        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
        cli = install_dir / "scripts" / "costmarshal.py"
        run([sys.executable, str(cli), "--help"], env)
        run([sys.executable, str(cli), "init-root"], env)

        runtime_root = codex_home / "costmarshal"
        assert_true((runtime_root / "config" / "agents.json").is_file(), "init-root should create default runtime config")
        assert_true((runtime_root / "memory" / "agent-memory.json").is_file(), "init-root should create runtime memory")

        install_prompt = (install_dir / "INSTALL_PROMPT.md").read_text(encoding="utf-8")
        uninstall_prompt = (install_dir / "UNINSTALL_PROMPT.md").read_text(encoding="utf-8")
        assert_true("https://github.com/yptang98/CostMarshal" in install_prompt, "install prompt should include GitHub URL")
        assert_true("Do not delete CostMarshal runtime state unless I explicitly confirm." in uninstall_prompt, "uninstall prompt should preserve runtime state by default")

        shutil.rmtree(install_dir)
        assert_true(not install_dir.exists(), "uninstall should remove installed skill directory")
        assert_true(runtime_root.exists(), "uninstall should preserve runtime state by default")

        print("install smoke ok: temporary state cleaned")
        return 0
    finally:
        resolved = temp.resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        if resolved == temp_root or temp_root not in resolved.parents:
            raise RuntimeError(f"Refusing to delete unexpected path: {resolved}")
        shutil.rmtree(resolved, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
