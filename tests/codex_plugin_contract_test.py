#!/usr/bin/env python3
"""Codex-native plugin/Skill packaging stays bound to the CostMarshal policy."""

from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.sync_plugin_package import PACKAGE, package_differences  # noqa: E402

PLUGIN_MANIFEST = ROOT / ".codex-plugin" / "plugin.json"
MARKETPLACE = ROOT / ".agents" / "plugins" / "marketplace.json"
PLUGIN_SKILL = ROOT / "skills" / "orchestrate-cost-aware-agents" / "SKILL.md"
PLUGIN_UI = PLUGIN_SKILL.parent / "agents" / "openai.yaml"
LEGACY_UI = ROOT / "agents" / "openai.yaml"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip().removeprefix("v")
    require(manifest.get("name") == "costmarshal", "plugin name must remain stable")
    require(manifest.get("version") == version, "plugin version must match VERSION")
    require(manifest.get("skills") == "./skills/", "plugin must expose its Skill root")
    require(
        manifest.get("repository") == "https://github.com/yptang98/CostMarshal",
        "plugin repository provenance is missing",
    )
    interface = manifest.get("interface") or {}
    require(interface.get("displayName") == "CostMarshal", "plugin display name drifted")
    require(
        set(interface.get("capabilities") or []) == {"Interactive", "Write"},
        "plugin capability declaration drifted",
    )
    prompts = interface.get("defaultPrompt")
    require(
        isinstance(prompts, list)
        and 1 <= len(prompts) <= 3
        and all(isinstance(prompt, str) and 0 < len(prompt) <= 128 for prompt in prompts),
        "plugin starter prompts are invalid",
    )

    marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    require(marketplace.get("name") == "costmarshal", "marketplace name drifted")
    entries = marketplace.get("plugins") or []
    require(len(entries) == 1, "marketplace must expose exactly one plugin")
    entry = entries[0]
    require(entry.get("name") == "costmarshal", "marketplace plugin id drifted")
    require(
        entry.get("source")
        == {"source": "local", "path": "./plugins/costmarshal"},
        "marketplace must install the curated plugin distribution",
    )
    require(
        entry.get("policy")
        == {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "marketplace policy drifted",
    )
    differences = package_differences()
    require(not differences, f"committed plugin package drifted: {differences[:8]}")
    packaged_manifest = json.loads(
        (PACKAGE / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    require(packaged_manifest == manifest, "packaged plugin manifest drifted")
    for forbidden in (".agents", ".git", ".github", "artifacts", "release", "tests"):
        require(not (PACKAGE / forbidden).exists(), f"plugin package includes {forbidden}")

    skill = PLUGIN_SKILL.read_text(encoding="utf-8")
    require(
        skill.startswith("---\nname: orchestrate-cost-aware-agents\n"),
        "Codex plugin Skill frontmatter is invalid",
    )
    require("../../SKILL.md" in skill, "plugin Skill must delegate to the root policy")
    require(
        (PLUGIN_SKILL.parent / "../../SKILL.md").resolve() == (ROOT / "SKILL.md").resolve(),
        "plugin Skill policy link escapes or misses the plugin root",
    )
    require("internal implementation" in skill, "plugin Skill must keep CLI details internal")
    for control in ("Set up CostMarshal", "Plan or explain", "Do new work", "Resume or recover", "Stop"):
        require(control in skill, f"plugin control plane lost {control!r}")

    ui = PLUGIN_UI.read_text(encoding="utf-8")
    require(
        'default_prompt: "Use $orchestrate-cost-aware-agents ' in ui,
        "Skill UI prompt must explicitly invoke the Skill",
    )
    require("allow_implicit_invocation: true" in ui, "natural-language invocation is disabled")
    legacy_ui = LEGACY_UI.read_text(encoding="utf-8")
    require(
        "allow_implicit_invocation: false" in legacy_ui,
        "legacy standalone Skill must be explicit-only",
    )

    root_policy = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    for invariant in ("low", "medium", "high", "leader", "ArchMarshal"):
        require(invariant in root_policy, f"root CostMarshal policy lost {invariant!r}")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    require("## Use from Codex" in readme, "Codex-native product surface is undocumented")
    require(
        "not as a requirement for ordinary users" in readme,
        "README still presents the CLI as the ordinary user interface",
    )
    install_prompt = (ROOT / "INSTALL_PROMPT.md").read_text(encoding="utf-8")
    require("codex plugin marketplace add" in install_prompt, "install prompt bypasses marketplace")
    require("codex plugin add costmarshal@costmarshal" in install_prompt, "install prompt bypasses plugin add")
    require("40-character" in install_prompt, "install prompt does not require a pinned commit")
    print("codex plugin contract ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
