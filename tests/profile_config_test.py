#!/usr/bin/env python3
"""Contract checks for safe, generic Codex provider profile generation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"
sys.path.insert(0, str(ROOT))

from costmarshal_v2.profile_binding import read_named_profile  # noqa: E402
from costmarshal_v2.actor_runner import (  # noqa: E402
    _isolated_codex_home,
    _write_inherited_provider_config,
)
from costmarshal_v2.profile_binding import (  # noqa: E402
    install_profile_snapshot,
    validate_profile_binding,
)


def run(*args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == expect, (result.returncode, result.stdout, result.stderr)
    return result


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="costmarshal-profile-") as raw:
        home = Path(raw)
        presets = json.loads(run("provider-presets").stdout)
        by_id = {
            item["preset_id"]: item for item in presets["presets"]
        }
        assert {
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "kimi-k3",
            "kimi-k2.6",
            "longcat-2.0",
            "mimo-v2.5",
            "mimo-v2.5-pro",
            "doubao-seed-2.0-lite",
        } == set(by_id)
        assert by_id["deepseek-v4-flash"]["api_multimodal"] is False
        assert by_id["longcat-2.0"]["wire_api"] == "responses"
        assert by_id["kimi-k2.6"]["api_multimodal"] is True
        assert by_id["kimi-k2.6"]["runtime_multimodal"] is False
        assert by_id["kimi-k2.6"]["effective_capabilities"] == []
        assert by_id["kimi-k2.6"]["codex_compatible"] is False
        assert by_id["kimi-k2.6"]["gateway_compatible"] is True
        assert "input:image" in by_id["kimi-k2.6"][
            "gateway_effective_capabilities"
        ]
        assert "input:video" not in by_id["kimi-k2.6"][
            "gateway_effective_capabilities"
        ]
        assert "input:video" not in by_id["kimi-k2.6"]["effective_capabilities"]
        assert "input:audio" in by_id["mimo-v2.5"]["api_capabilities"]
        assert "input:audio" not in by_id["mimo-v2.5"]["effective_capabilities"]
        assert "input:image" in by_id["mimo-v2.5"]["effective_capabilities"]

        configured_preset = json.loads(
            run(
                "configure-provider",
                "--codex-home",
                str(home),
                "--preset",
                "mimo-v2.5",
                "--tier",
                "medium",
            ).stdout
        )
        assert configured_preset["profile"] == "mimo"
        assert configured_preset["env_key"] == "MIMO_API_KEY"
        assert configured_preset["catalog_provider"]["capabilities"] == by_id[
            "mimo-v2.5"
        ]["effective_capabilities"]
        assert "input:video" not in configured_preset["catalog_provider"]["capabilities"]
        mimo_text = (home / "mimo.config.toml").read_text(encoding="utf-8")
        assert 'base_url = "https://api.xiaomimimo.com/v1"' in mimo_text
        assert 'wire_api = "responses"' in mimo_text
        assert "MIMO_API_KEY" in mimo_text
        assert "sk-" not in mimo_text

        unsupported = run(
            "configure-provider",
            "--codex-home",
            str(home),
            "--preset",
            "kimi-k2.6",
            "--dry-run",
            expect=1,
        )
        assert "Responses-only Codex worker" in unsupported.stderr

        gateway_kimi = json.loads(
            run(
                "configure-provider",
                "--codex-home",
                str(home),
                "--preset",
                "kimi-k2.6",
                "--profile",
                "kimi-gateway",
                "--via-production-gateway",
                "--dry-run",
            ).stdout
        )
        assert gateway_kimi["requires_production_gateway"] is True
        assert gateway_kimi["catalog_provider"]["runtime_adapter"] == (
            "costmarshal-gateway-v1"
        )
        assert "input:image" in gateway_kimi["catalog_provider"]["capabilities"]
        assert "input:video" not in gateway_kimi["catalog_provider"]["capabilities"]
        assert gateway_kimi["dry_run"] is True

        custom_gateway = run(
            "configure-provider",
            "--codex-home",
            str(home),
            "--profile",
            "custom-gateway",
            "--provider-id",
            "custom",
            "--base-url",
            "https://example.test/v1",
            "--model",
            "model",
            "--env-key",
            "API_KEY",
            "--via-production-gateway",
            "--dry-run",
            expect=1,
        )
        assert "requires a reviewed --preset" in custom_gateway.stderr

        conflict = run(
            "configure-provider",
            "--codex-home",
            str(home),
            "--preset",
            "mimo",
            "--provider-id",
            "other",
            "--dry-run",
            expect=1,
        )
        assert "cannot be combined" in conflict.stderr

        dry = run(
            "configure-provider",
            "--codex-home",
            str(home),
            "--profile",
            "medium",
            "--provider-id",
            "mid-api",
            "--display-name",
            "Medium API",
            "--base-url",
            "https://example.test/v1",
            "--model",
            "medium-model",
            "--env-key",
            "MEDIUM_API_KEY",
            "--wire-api",
            "responses",
            "--dry-run",
        )
        payload = json.loads(dry.stdout)
        assert payload["dry_run"] is True
        assert not (home / "medium.config.toml").exists()

        run(
            "configure-provider",
            "--codex-home",
            str(home),
            "--profile",
            "medium",
            "--provider-id",
            "mid-api",
            "--base-url",
            "https://example.test/v1",
            "--model",
            "medium-model",
            "--env-key",
            "MEDIUM_API_KEY",
        )
        text = (home / "medium.config.toml").read_text(encoding="utf-8")
        assert 'model_provider = "mid-api"' in text
        assert '[model_providers.mid-api]' in text
        assert 'env_key = "MEDIUM_API_KEY"' in text
        assert "secret" not in text.lower()

        duplicate = run(
            "configure-provider",
            "--codex-home",
            str(home),
            "--profile",
            "medium",
            "--provider-id",
            "mid-api",
            "--base-url",
            "https://example.test/v1",
            "--model",
            "medium-model",
            "--env-key",
            "MEDIUM_API_KEY",
            expect=1,
        )
        assert "use --force" in duplicate.stderr

        reserved = run(
            "configure-provider",
            "--codex-home",
            str(home),
            "--profile",
            "bad",
            "--provider-id",
            "openai",
            "--base-url",
            "https://example.test/v1",
            "--model",
            "model",
            "--env-key",
            "API_KEY",
            expect=1,
        )
        assert "reserved" in reserved.stderr

        credential_url = run(
            "configure-provider",
            "--codex-home",
            str(home),
            "--profile",
            "bad-url",
            "--provider-id",
            "custom",
            "--base-url",
            "https://user:password@example.test/v1",
            "--model",
            "model",
            "--env-key",
            "API_KEY",
            expect=1,
        )
        assert "credentials" in credential_url.stderr

        dotted = run(
            "configure-provider",
            "--codex-home",
            str(home),
            "--profile",
            "medium.v2",
            "--provider-id",
            "custom",
            "--base-url",
            "https://example.test/v1",
            "--model",
            "model",
            "--env-key",
            "API_KEY",
            "--dry-run",
        )
        assert json.loads(dotted.stdout)["profile"] == "medium.v2"

        boundary = run(
            "configure-provider",
            "--codex-home",
            str(home),
            f"--profile={'x' * 64}",
            "--provider-id",
            "custom",
            "--base-url",
            "https://example.test/v1",
            "--model",
            "model",
            "--env-key",
            "API_KEY",
            "--dry-run",
        )
        assert json.loads(boundary.stdout)["profile"] == "x" * 64

        for invalid_profile in (
            "-bad",
            "bad_",
            "x" * 65,
            "CON",
            "nul",
            "Com1",
            "LPT9",
            "CON.v2",
            "nul.backup",
            "Com1.prod",
        ):
            invalid = run(
                "configure-provider",
                "--codex-home",
                str(home),
                f"--profile={invalid_profile}",
                "--provider-id",
                "custom",
                "--base-url",
                "https://example.test/v1",
                "--model",
                "model",
                "--env-key",
                "API_KEY",
                "--dry-run",
                expect=1,
            )
            assert "profile name must be 1-64 characters" in invalid.stderr

        default_user_home = home / "default-user"
        default_user_home.mkdir()
        with patch.dict(
            os.environ,
            {
                "CODEX_HOME": "",
                "HOME": str(default_user_home),
                "USERPROFILE": str(default_user_home),
            },
            clear=False,
        ):
            configured = run(
                "configure-provider",
                "--profile",
                "roundtrip",
                "--provider-id",
                "custom",
                "--base-url",
                "https://example.test/v1",
                "--model",
                "model",
                "--env-key",
                "API_KEY",
            )
            configured_path = Path(json.loads(configured.stdout)["path"])
            material = read_named_profile(
                "roundtrip",
                expected_env_key="API_KEY",
                snapshot_relpath="profile-snapshots/roundtrip/config.toml",
            )
        assert configured_path == (default_user_home / ".codex" / "roundtrip.config.toml").resolve()
        assert material is not None and material[0] == configured_path.read_bytes()

        # Real-world Codex profiles may carry standard non-secret keys
        # (model_context_window, model_catalog_json) and inherit the provider
        # endpoint/key contract from the shared config.toml.  Routing evidence
        # must accept them instead of rejecting the user's actual config.
        inherited_home = home / "inherited-home"
        inherited_home.mkdir()
        (inherited_home / "config.toml").write_text(
            '[model_providers.deepseek]\n'
            'name = "DeepSeek"\n'
            'base_url = "https://api.deepseek.com"\n'
            'wire_api = "responses"\n'
            'env_key = "DEEPSEEK_API_KEY"\n',
            encoding="utf-8",
        )
        (inherited_home / "deepseek.config.toml").write_text(
            'model = "deepseek-v4-pro"\n'
            'model_provider = "deepseek"\n'
            'model_reasoning_effort = "high"\n'
            'model_context_window = 1048576\n'
            'model_catalog_json = "C:/Users/example/.codex/models.json"\n',
            encoding="utf-8",
        )
        with patch.dict(
            os.environ,
            {"CODEX_HOME": str(inherited_home)},
            clear=False,
        ):
            inherited = read_named_profile(
                "deepseek",
                expected_env_key="DEEPSEEK_API_KEY",
                snapshot_relpath="profile-snapshots/deepseek/config.toml",
            )
        assert inherited is not None
        assert inherited[1]["status"] == "available"
        assert inherited[1]["provider_identity"] == "deepseek"
        assert inherited[1]["base_url"] == "https://api.deepseek.com"
        assert inherited[1]["wire_api"] == "responses"
        assert inherited[1]["env_key"] == "DEEPSEEK_API_KEY"

        # Unknown or credential-bearing keys remain fail-closed.
        (inherited_home / "deepseek.config.toml").write_text(
            'model = "deepseek-v4-pro"\n'
            'model_provider = "deepseek"\n'
            '[model_providers.deepseek]\n'
            'name = "DeepSeek"\n'
            'base_url = "https://api.deepseek.com"\n'
            'env_key = "DEEPSEEK_API_KEY"\n'
            'headers = { Authorization = "Bearer sk-test" }\n',
            encoding="utf-8",
        )
        with patch.dict(
            os.environ,
            {"CODEX_HOME": str(inherited_home)},
            clear=False,
        ):
            try:
                read_named_profile(
                    "deepseek",
                    expected_env_key="DEEPSEEK_API_KEY",
                    snapshot_relpath="profile-snapshots/deepseek/config.toml",
                )
                rejected = False
            except Exception:
                rejected = True
        assert rejected

        # A worker's isolated home must be self-contained: when the bound
        # profile inherits [model_providers.<id>] from config.toml, the runner
        # recreates exactly the reviewed provider row so Codex exec can resolve
        # it without the host config or its secrets.
        (inherited_home / "deepseek.config.toml").write_text(
            'model = "deepseek-v4-pro"\n'
            'model_provider = "deepseek"\n'
            'model_reasoning_effort = "high"\n'
            'model_context_window = 1048576\n'
            'model_catalog_json = "C:/Users/example/.codex/models.json"\n',
            encoding="utf-8",
        )
        layout_root = Path(raw) / "isolated-runtime"
        project_dir = layout_root / "projects" / "p"
        project_dir.mkdir(parents=True)
        layout = SimpleNamespace(
            root=layout_root,
            project_dir=project_dir,
        )
        with patch.dict(
            os.environ,
            {"CODEX_HOME": str(inherited_home)},
            clear=False,
        ):
            material = read_named_profile(
                "deepseek",
                expected_env_key="DEEPSEEK_API_KEY",
                snapshot_relpath="profile-snapshots/isolated/config.toml",
            )
        assert material is not None
        payload, binding = material
        binding = validate_profile_binding(binding, require_available=True)
        install_profile_snapshot(layout_root, payload, binding)
        actor = {
            "id": "agent-isolated",
            "role": "agent",
            "profile": "deepseek",
            "tier": "medium",
            "profile_binding": binding,
        }
        isolated = _isolated_codex_home(layout, actor, {})
        assert (isolated / "deepseek.config.toml").is_file()
        inherited_config = isolated / "config.toml"
        assert inherited_config.is_file()
        inherited_text = inherited_config.read_text(encoding="utf-8")
        assert "base_url = \"https://api.deepseek.com\"" in inherited_text
        assert "wire_api = \"responses\"" in inherited_text
        assert "env_key = \"DEEPSEEK_API_KEY\"" in inherited_text
        assert "sk-" not in inherited_text

        # No inherited row means no fabricated provider config.
        empty_target = Path(raw) / "empty-isolated"
        empty_target.mkdir()
        _write_inherited_provider_config(
            empty_target,
            {
                "provider_identity": "deepseek",
                "base_url": None,
                "wire_api": None,
                "env_key": None,
            },
        )
        assert not (empty_target / "config.toml").exists()
    print("provider profile contract ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
