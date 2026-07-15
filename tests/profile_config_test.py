#!/usr/bin/env python3
"""Contract checks for safe, generic Codex provider profile generation."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"


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
    print("provider profile contract ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
