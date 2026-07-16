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
MINIMUM_PYTHON = (3, 11)


IGNORED_DIRS = {
    ".git",
    ".github",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "costmarshal",
    "costmarshal-v2",
    "projects",
    "memory",
    "config",
    "artifacts",
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
    assert_true(
        sys.version_info >= MINIMUM_PYTHON,
        "CostMarshal requires Python 3.11+ for installation and runtime",
    )
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-install-smoke-"))
    try:
        codex_home = temp / "codex-home"
        skills_dir = codex_home / "skills"
        install_dir = skills_dir / "costmarshal"
        runtime_root = codex_home / "costmarshal-v2"
        legacy_runtime_root = codex_home / "costmarshal"
        skills_dir.mkdir(parents=True)
        runtime_root.mkdir(parents=True)
        legacy_runtime_root.mkdir(parents=True)
        (runtime_root / "runtime-marker.txt").write_text("preserve me\n", encoding="utf-8")
        (legacy_runtime_root / "legacy-marker.txt").write_text("preserve legacy\n", encoding="utf-8")
        install_dir.mkdir(parents=True)
        (install_dir / "VERSION").write_text("v0.0.1\n", encoding="utf-8")
        (install_dir / "SKILL.md").write_text("---\nname: costmarshal\n---\n# Old\n", encoding="utf-8")
        (install_dir / ".env").write_text("DO_NOT_COPY_OR_PRINT=secret\n", encoding="utf-8")

        old_version = (install_dir / "VERSION").read_text(encoding="utf-8").strip()
        backup_dir = skills_dir / "costmarshal.backup-smoke"
        shutil.move(str(install_dir), str(backup_dir))
        shutil.copytree(SOURCE, install_dir, ignore=ignore_install_artifacts)

        assert_true(old_version == "v0.0.1", "update flow should detect old installed version")
        assert_true(backup_dir.exists(), "update flow should back up an existing install")
        assert_true((backup_dir / ".env").is_file(), "backup should preserve old local files")
        assert_true((install_dir / "SKILL.md").is_file(), "installed skill should include SKILL.md")
        assert_true((install_dir / "scripts" / "costmarshal.py").is_file(), "installed skill should include CLI")
        assert_true((install_dir / "container" / "worker" / "Dockerfile").is_file(), "installed skill should include worker image source")
        assert_true((install_dir / "references" / "backtest.md").is_file(), "installed skill should include release references")
        assert_true((install_dir / "tests" / "release" / "run_release_gates.py").is_file(), "installed skill should include release gates")
        assert_true((install_dir / "release" / "evidence-policy.json").is_file(), "installed skill should include release trust policy")
        assert_true((install_dir / "VERSION").read_text(encoding="utf-8").strip() != old_version, "update should install the new version")
        assert_true(not (install_dir / ".git").exists(), "install copy should not include .git")
        assert_true(not (install_dir / ".github").exists(), "install copy should not include repository CI metadata")
        assert_true(not any(install_dir.rglob("*.env")), "install copy should not include .env files")
        assert_true(not (install_dir / "artifacts").exists(), "install copy should not include generated evidence")

        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
        cli = install_dir / "scripts" / "costmarshal.py"
        run([sys.executable, str(cli), "--help"], env)
        init = run(
            [
                sys.executable,
                str(cli),
                "init",
                "--name",
                "install-smoke",
                "--objective",
                "Validate CostMarshal v2 install",
                "--backend",
                "local",
            ],
            env,
        )
        import json

        project = Path(json.loads(init.stdout)["project"])
        run([sys.executable, str(cli), "run-scheduler", "--project", str(project), "--once"], env)
        dashboard = run([sys.executable, str(cli), "dashboard", "--project", str(project), "--format", "json"], env)
        assert_true(any(row["id"] == "scheduler" for row in json.loads(dashboard.stdout)["processes"]), "dashboard should show scheduler process state")
        run([sys.executable, str(cli), "validate", "--project", str(project)], env)

        assert_true((project / "project.json").is_file(), "v2 init should create project state")
        assert_true((project / "scheduler" / "session.json").is_file(), "v2 init should create scheduler session state")
        assert_true((runtime_root / "runtime-marker.txt").read_text(encoding="utf-8") == "preserve me\n", "update should preserve runtime state")
        assert_true((legacy_runtime_root / "legacy-marker.txt").read_text(encoding="utf-8") == "preserve legacy\n", "update should preserve legacy runtime state")

        install_prompt = (install_dir / "INSTALL_PROMPT.md").read_text(encoding="utf-8")
        uninstall_prompt = (install_dir / "UNINSTALL_PROMPT.md").read_text(encoding="utf-8")
        assert_true("https://github.com/yptang98/CostMarshal" in install_prompt, "install prompt should include GitHub URL")
        assert_true("already exists, treat this as an update" in install_prompt, "install prompt should describe update behavior")
        assert_true("Preserve $CODEX_HOME/costmarshal-v2" in install_prompt, "install prompt should preserve v2 runtime state during updates")
        assert_true("legacy $CODEX_HOME/costmarshal" in install_prompt, "install prompt should preserve legacy runtime state during updates")
        assert_true("Python 3.11+" in install_prompt, "install prompt should require Python 3.11+")
        assert_true("Do not delete CostMarshal runtime state unless I explicitly confirm." in uninstall_prompt, "uninstall prompt should preserve runtime state by default")

        shutil.rmtree(install_dir)
        assert_true(not install_dir.exists(), "uninstall should remove installed skill directory")
        assert_true(runtime_root.exists(), "uninstall should preserve runtime state by default")
        assert_true(legacy_runtime_root.exists(), "uninstall should preserve legacy runtime state by default")

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
