#!/usr/bin/env python3
"""End-to-end contract for provider presets and committed image routing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"
sys.path.insert(0, str(ROOT))

from costmarshal_v2.actor_runner import build_codex_argv  # noqa: E402
from costmarshal_v2.paths import ProjectLayout  # noqa: E402
from costmarshal_v2.provider_presets import resolve_provider_preset  # noqa: E402
from costmarshal_v2.routing import validate_provider_catalog  # noqa: E402
from costmarshal_v2.state import load_project, load_task  # noqa: E402


def run(temp: Path, *args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
    env["CODEX_HOME"] = str(temp / "codex-home")
    result = subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if ok:
        assert result.returncode == 0, (result.stdout, result.stderr)
    else:
        assert result.returncode != 0, result.stdout
    return result


def main() -> int:
    gateway_provider = resolve_provider_preset("kimi-k3").catalog_provider(
        tier="medium",
        profile="kimi-gateway",
        via_production_gateway=True,
    )
    normalized_gateway = validate_provider_catalog(
        {"schema_version": 1, "providers": [gateway_provider]}
    )["providers"][0]
    assert normalized_gateway["runtime_adapter"] == "costmarshal-gateway-v1"
    assert "input:image" in normalized_gateway["capabilities"]

    with tempfile.TemporaryDirectory(prefix="costmarshal-provider-capability-") as raw:
        temp = Path(raw)
        workspace = temp / "workspace"
        workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        subprocess.run(
            ["git", "-C", str(workspace), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(workspace), "config", "user.name", "CostMarshal Test"],
            check=True,
        )
        assets = workspace / "assets"
        assets.mkdir()
        image = assets / "reference.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\ncontract-test")
        subprocess.run(["git", "-C", str(workspace), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(workspace), "commit", "-qm", "image fixture"],
            check=True,
        )

        presets = (
            ("mimo-v2.5", "low", "mimo"),
            ("doubao-seed-2.0-lite", "high", "doubao"),
        )
        catalog = {
            "schema_version": 1,
            "providers": [
                resolve_provider_preset(preset).catalog_provider(
                    tier=tier,
                    profile=profile,
                )
                for preset, tier, profile in presets
            ],
        }
        catalog_path = temp / "providers.json"
        catalog_path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        init = json.loads(
            run(
                temp,
                "init",
                "--name",
                "multimodal",
                "--objective",
                "route a committed image",
                "--workspace",
                str(workspace),
                "--provider-catalog",
                str(catalog_path),
                "--routing-objective",
                "cost-only",
                "--backend",
                "local",
                "--governance",
                "off",
                "--allow-unsafe-native-workers",
            ).stdout
        )
        project_dir = Path(init["project"])
        created = json.loads(
            run(
                temp,
                "new-task",
                "--project",
                str(project_dir),
                "--title",
                "Read image",
                "--purpose",
                "Verify multimodal routing",
                "--input-image",
                "assets/reference.png",
            ).stdout
        )
        assert created["task_id"] == "V2-0001"

        layout = ProjectLayout(root=temp / "runtime", project_dir=project_dir)
        task = load_task(layout, "V2-0001")
        assert task["input_images"] == ["assets/reference.png"]
        assert task["allowed_context"] == ["assets/reference.png"]
        assert "input:image" in task["required_capabilities"]
        assert task["route_preview"]["provider_id"] == "mimo"
        brief = (project_dir / "tasks" / "V2-0001" / "brief.md").read_text(
            encoding="utf-8"
        )
        assert "## Input Images" in brief
        assert "assets/reference.png" in brief

        actor = {
            "role": "agent",
            "task_id": "V2-0001",
            "profile": "mimo",
            "model": "mimo-v2.5",
            "runner": {"approval_policy": "never", "sandbox": "read-only"},
        }
        report = project_dir / "reports" / "test.md"
        argv = build_codex_argv(
            layout,
            actor,
            load_project(layout),
            report,
            execution_workspace=workspace,
            sandbox="read-only",
        )
        image_index = argv.index("--image")
        assert argv[image_index + 1] == str(image.resolve())
        assert argv[-1] == "-"

        untracked = assets / "untracked.png"
        untracked.write_bytes(b"\x89PNG\r\n\x1a\nuntracked")
        rejected = run(
            temp,
            "new-task",
            "--project",
            str(project_dir),
            "--title",
            "Reject untracked",
            "--purpose",
            "Fail closed",
            "--input-image",
            "assets/untracked.png",
            ok=False,
        )
        assert "committed HEAD" in rejected.stderr

    print("provider capability contract ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
