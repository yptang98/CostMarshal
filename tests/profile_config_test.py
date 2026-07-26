#!/usr/bin/env python3
"""Contract checks for safe, generic Codex provider profile generation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"
sys.path.insert(0, str(ROOT))

from costmarshal_v2.profile_binding import read_named_profile  # noqa: E402


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
    print("provider profile contract ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
