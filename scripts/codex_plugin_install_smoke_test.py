#!/usr/bin/env python3
"""Real Codex marketplace/plugin install smoke in an isolated CODEX_HOME."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1]
MINIMUM_PYTHON = (3, 11)
MINIMUM_CODEX = (0, 144, 1)
PLUGIN_ID = "costmarshal@costmarshal"
COMMAND_TIMEOUT_SECONDS = 180


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run(
    command: list[str],
    env: dict[str, str],
    *,
    parse_json: bool = False,
) -> subprocess.CompletedProcess[str] | object:
    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"Command failed: {' '.join(command)}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    if not parse_json:
        return result
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"Command returned malformed JSON: {' '.join(command)}\n{result.stdout}"
        ) from exc


def find_codex() -> str:
    candidates = ("codex.cmd", "codex") if os.name == "nt" else ("codex",)
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise AssertionError(
        "Codex CLI is required for the product install smoke; install "
        "@openai/codex 0.144.1 or newer"
    )


def assert_codex_version(codex: str, env: dict[str, str]) -> None:
    result = run([codex, "--version"], env)
    assert isinstance(result, subprocess.CompletedProcess)
    match = re.search(r"codex-cli\s+(\d+)\.(\d+)\.(\d+)", result.stdout)
    assert_true(match is not None, f"unrecognized Codex version: {result.stdout!r}")
    version = tuple(int(group) for group in match.groups())
    assert_true(
        version >= MINIMUM_CODEX,
        f"Codex {MINIMUM_CODEX!r}+ is required for plugin marketplace smoke",
    )


def require_installed_files(installed: Path) -> None:
    required = (
        ".codex-plugin/plugin.json",
        "CHANGELOG.md",
        "SECURITY.md",
        "SKILL.md",
        "skills/orchestrate-cost-aware-agents/SKILL.md",
        "scripts/costmarshal.py",
        "scripts/costmarshal_actor.py",
        "costmarshal_v2/cli.py",
        "costmarshal_v2/windows_job_supervisor.py",
        "references/protocol.md",
        "container/worker/.dockerignore",
        "container/worker/Dockerfile",
        "container/worker/costmarshal-escape-probe.js",
        "container/worker/costmarshal-isolation-canary.js",
        "container/worker/costmarshal-worker.js",
    )
    for relative in required:
        assert_true((installed / relative).is_file(), f"installed plugin lost {relative}")
    forbidden_names = {
        ".agents",
        ".env",
        ".git",
        ".github",
        "__pycache__",
        "artifacts",
        "plugins",
        "secrets.json",
        "tests",
        "release",
    }
    forbidden_suffixes = {".pyc", ".pyo"}
    for path in installed.rglob("*"):
        relative = path.relative_to(installed)
        metadata = path.lstat()
        attributes = getattr(metadata, "st_file_attributes", 0)
        is_reparse = bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        assert_true(
            not path.is_symlink() and not is_reparse,
            f"plugin cache contains a linked or redirected entry: {relative}",
        )
        assert_true(
            stat.S_ISDIR(metadata.st_mode)
            or (stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1),
            f"plugin cache contains a non-regular or hard-linked entry: {relative}",
        )
        lowered_parts = {part.casefold() for part in relative.parts}
        assert_true(
            not lowered_parts.intersection(forbidden_names),
            f"plugin cache contains forbidden path: {relative}",
        )
        assert_true(
            path.suffix.casefold() not in forbidden_suffixes,
            f"plugin cache contains bytecode: {relative}",
        )
    assert_true(
        not (installed / "scripts" / "mc.py").exists(),
        "plugin cache must not include legacy CLI",
    )


def remove_tree_strict(path: Path) -> None:
    def remove_readonly(action, entry: str, _error) -> None:
        os.chmod(entry, 0o700)
        action(entry)

    shutil.rmtree(path, onerror=remove_readonly)
    assert_true(not path.exists(), f"temporary smoke root survived cleanup: {path}")


def main() -> int:
    assert_true(
        sys.version_info >= MINIMUM_PYTHON,
        "CostMarshal requires Python 3.11+ for its hidden runtime",
    )
    codex = find_codex()
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-plugin-smoke-"))
    try:
        package_root = temp / "marketplace-v3"
        legacy_package_root = temp / "marketplace-v2"
        codex_home = temp / "codex-home"
        runtime_root = codex_home / "costmarshal-v2"
        legacy_runtime_root = codex_home / "costmarshal"
        package_root.mkdir()
        shutil.copytree(SOURCE / ".agents", package_root / ".agents")
        shutil.copytree(SOURCE / "plugins", package_root / "plugins")
        for repository_only in (".git", ".github", "artifacts"):
            sentinel = package_root / repository_only
            sentinel.mkdir()
            (sentinel / "must-not-install.txt").write_text(
                "marketplace transport metadata\n", encoding="utf-8"
            )
        legacy_package_root.mkdir()
        shutil.copytree(SOURCE / ".agents", legacy_package_root / ".agents")
        for source in sorted((SOURCE / "plugins" / "costmarshal").iterdir()):
            destination = legacy_package_root / source.name
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
        legacy_marketplace_path = (
            legacy_package_root / ".agents" / "plugins" / "marketplace.json"
        )
        legacy_marketplace = json.loads(
            legacy_marketplace_path.read_text(encoding="utf-8")
        )
        legacy_marketplace["plugins"][0]["source"] = {
            "source": "local",
            "path": "./",
        }
        legacy_marketplace_path.write_text(
            json.dumps(legacy_marketplace, indent=2) + "\n",
            encoding="utf-8",
        )
        legacy_manifest_path = legacy_package_root / ".codex-plugin" / "plugin.json"
        legacy_manifest = json.loads(legacy_manifest_path.read_text(encoding="utf-8"))
        legacy_manifest["version"] = "2.4.0-beta"
        legacy_manifest_path.write_text(
            json.dumps(legacy_manifest, indent=2) + "\n", encoding="utf-8"
        )
        codex_home.mkdir()
        runtime_root.mkdir()
        legacy_runtime_root.mkdir()
        (runtime_root / "runtime-marker.txt").write_text("preserve me\n", encoding="utf-8")
        (legacy_runtime_root / "legacy-marker.txt").write_text(
            "preserve legacy\n", encoding="utf-8"
        )

        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
        assert_codex_version(codex, env)
        legacy_added = run(
            [
                codex,
                "plugin",
                "marketplace",
                "add",
                str(legacy_package_root),
                "--json",
            ],
            env,
            parse_json=True,
        )
        assert isinstance(legacy_added, dict)
        assert_true(
            legacy_added.get("marketplaceName") == "costmarshal",
            "legacy marketplace name drifted",
        )
        legacy_install = run(
            [codex, "plugin", "add", PLUGIN_ID, "--json"], env, parse_json=True
        )
        assert isinstance(legacy_install, dict)
        legacy_installed = Path(str(legacy_install.get("installedPath", ""))).resolve()
        assert_true(
            (legacy_installed / ".codex-plugin" / "plugin.json").is_file(),
            "legacy root-layout plugin was not installed",
        )
        assert_true(
            (legacy_installed / ".agents").is_dir()
            and not (legacy_installed / "plugins").exists(),
            "legacy fixture did not exercise the root-layout package",
        )
        legacy_listed = run([codex, "plugin", "list", "--json"], env, parse_json=True)
        assert isinstance(legacy_listed, dict)
        legacy_rows = legacy_listed.get("installed", [])
        assert_true(
            len(legacy_rows) == 1 and legacy_rows[0].get("version") == "2.4.0-beta",
            "legacy fixture was not installed before upgrade",
        )
        run([codex, "plugin", "remove", PLUGIN_ID, "--json"], env, parse_json=True)
        assert_true(not legacy_installed.exists(), "legacy plugin cache survived removal")
        run(
            [codex, "plugin", "marketplace", "remove", "costmarshal", "--json"],
            env,
            parse_json=True,
        )
        assert_true(
            (runtime_root / "runtime-marker.txt").is_file()
            and (legacy_runtime_root / "legacy-marker.txt").is_file(),
            "pinned marketplace replacement changed runtime state",
        )

        added = run(
            [codex, "plugin", "marketplace", "add", str(package_root), "--json"],
            env,
            parse_json=True,
        )
        assert isinstance(added, dict)
        assert_true(added.get("marketplaceName") == "costmarshal", "marketplace name drifted")

        marketplaces = run(
            [codex, "plugin", "marketplace", "list", "--json"], env, parse_json=True
        )
        assert isinstance(marketplaces, dict)
        assert_true(
            any(row.get("name") == "costmarshal" for row in marketplaces.get("marketplaces", [])),
            "CostMarshal marketplace was not registered",
        )
        available = run(
            [codex, "plugin", "list", "--marketplace", "costmarshal", "--available", "--json"],
            env,
            parse_json=True,
        )
        assert isinstance(available, dict)
        candidates = available.get("available", [])
        assert_true(len(candidates) == 1, "marketplace must expose exactly one plugin")
        assert_true(candidates[0].get("pluginId") == PLUGIN_ID, "plugin id drifted")
        assert_true(not candidates[0].get("installed"), "plugin unexpectedly preinstalled")

        installed_result = run(
            [codex, "plugin", "add", PLUGIN_ID, "--json"], env, parse_json=True
        )
        assert isinstance(installed_result, dict)
        installed = Path(str(installed_result.get("installedPath", ""))).resolve()
        assert_true(codex_home.resolve() in installed.parents, "plugin escaped isolated CODEX_HOME")
        require_installed_files(installed)

        listed = run([codex, "plugin", "list", "--json"], env, parse_json=True)
        assert isinstance(listed, dict)
        rows = listed.get("installed", [])
        assert_true(len(rows) == 1, "Codex must list exactly one installed plugin")
        assert_true(rows[0].get("pluginId") == PLUGIN_ID, "installed plugin id drifted")
        assert_true(rows[0].get("installed") is True, "plugin is not installed")
        assert_true(rows[0].get("enabled") is True, "plugin is not enabled")
        manifest = json.loads((installed / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        assert_true(rows[0].get("version") == manifest.get("version"), "installed version drifted")

        cli = installed / "scripts" / "costmarshal.py"
        init = run(
            [
                sys.executable,
                str(cli),
                "init",
                "--name",
                "plugin-smoke",
                "--objective",
                "Validate the Codex-native CostMarshal plugin",
                "--backend",
                "local",
            ],
            env,
            parse_json=True,
        )
        assert isinstance(init, dict)
        project = Path(str(init["project"]))
        run(
            [
                sys.executable,
                str(cli),
                "route",
                "--project",
                str(project),
                "--task-type",
                "analysis",
                "--risk",
                "low",
                "--difficulty",
                "normal",
                "--estimated-input-tokens",
                "1000",
                "--estimated-output-tokens",
                "100",
            ],
            env,
            parse_json=True,
        )
        run(
            [sys.executable, str(cli), "run-scheduler", "--project", str(project), "--once"],
            env,
            parse_json=True,
        )
        dashboard = run(
            [sys.executable, str(cli), "dashboard", "--project", str(project), "--format", "json"],
            env,
            parse_json=True,
        )
        assert isinstance(dashboard, dict)
        assert_true(
            any(row.get("id") == "scheduler" for row in dashboard.get("processes", [])),
            "dashboard should show scheduler durable state",
        )
        run(
            [sys.executable, str(cli), "recover", "--project", str(project)],
            env,
            parse_json=True,
        )

        run([codex, "plugin", "remove", PLUGIN_ID, "--json"], env, parse_json=True)
        assert_true(not installed.exists(), "plugin removal should clear the installed cache")
        assert_true(
            (runtime_root / "runtime-marker.txt").read_text(encoding="utf-8") == "preserve me\n",
            "plugin removal changed v2 runtime state",
        )
        assert_true(
            (legacy_runtime_root / "legacy-marker.txt").read_text(encoding="utf-8")
            == "preserve legacy\n",
            "plugin removal changed legacy runtime state",
        )

        install_prompt = (SOURCE / "INSTALL_PROMPT.md").read_text(encoding="utf-8")
        assert_true("codex plugin marketplace add" in install_prompt, "install prompt bypasses marketplace")
        assert_true("codex plugin marketplace remove costmarshal" in install_prompt, "install prompt lacks pinned update flow")
        assert_true(f"codex plugin add {PLUGIN_ID}" in install_prompt, "install prompt bypasses plugin add")
        assert_true("40-character" in install_prompt, "install prompt does not pin a Git commit")
        assert_true("Do not require me to type Python" in install_prompt, "install prompt exposes CLI UX")
        print(
            "codex plugin install smoke ok: upgraded pinned marketplace, installed, "
            "exercised, removed, state preserved"
        )
        return 0
    finally:
        resolved = temp.resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        if resolved == temp_root or temp_root not in resolved.parents:
            raise RuntimeError(f"Refusing to delete unexpected path: {resolved}")
        remove_tree_strict(resolved)


if __name__ == "__main__":
    raise SystemExit(main())
