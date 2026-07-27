#!/usr/bin/env python3
"""End-to-end provider drift downgrade and reviewed recovery contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"


def run(
    temporary: Path,
    *args: str,
    ok: bool = True,
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["COSTMARSHAL_V2_HOME"] = str(temporary / "runtime")
    environment["CODEX_HOME"] = str(temporary / "codex-home")
    result = subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=ROOT,
        env=environment,
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
    with tempfile.TemporaryDirectory(
        prefix="costmarshal-provider-metadata-"
    ) as raw:
        temporary = Path(raw)
        workspace = temporary / "workspace"
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
        (workspace / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(workspace), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(workspace), "commit", "-qm", "fixture"],
            check=True,
        )
        initialized = json.loads(
            run(
                temporary,
                "init",
                "--name",
                "provider-metadata",
                "--objective",
                "prove provider drift lifecycle",
                "--workspace",
                str(workspace),
                "--backend",
                "local",
                "--governance",
                "off",
            ).stdout
        )
        project = Path(initialized["project"])
        digest = "sha256:" + "1" * 64
        observation = json.loads(
            run(
                temporary,
                "record-provider-observation",
                "--project",
                str(project),
                "--provider",
                "longcat",
                "--source",
                "https://longcat.chat/platform/docs/api/chat.html",
                "--evidence-sha256",
                digest,
                "--api-schema",
                "drift",
                "--pricing",
                "match",
                "--capabilities",
                "match",
                "--behavior",
                "match",
                "--apply",
                "--command-id",
                "provider-drift-1",
            ).stdout
        )["observation"]
        providers = json.loads(
            run(
                temporary,
                "providers",
                "--project",
                str(project),
            ).stdout
        )
        assert providers["provider_metadata"]["longcat"]["state"] == "blocked"
        longcat = next(
            row
            for row in providers["catalog"]["providers"]
            if row["provider_id"] == "longcat"
        )
        assert longcat["enabled"] is False

        catalog = json.loads((project / "project.json").read_text(encoding="utf-8"))[
            "provider_catalog"
        ]
        catalog_path = temporary / "reviewed-catalog.json"
        catalog_path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        expiry = (
            datetime.now(timezone.utc) + timedelta(days=30)
        ).isoformat().replace("+00:00", "Z")
        review = json.loads(
            run(
                temporary,
                "review-provider-metadata",
                "--project",
                str(project),
                "--provider",
                "longcat",
                "--catalog",
                str(catalog_path),
                "--observation",
                observation["observation_id"],
                "--approved-by",
                "release-reviewer",
                "--expires-at",
                expiry,
                "--apply",
                "--command-id",
                "provider-review-1",
            ).stdout
        )
        assert review["review"]["observation_ids"] == [
            observation["observation_id"]
        ]
        status = json.loads(
            run(
                temporary,
                "provider-metadata-status",
                "--project",
                str(project),
                "--provider",
                "longcat",
            ).stdout
        )
        assert status["providers"]["longcat"]["state"] == "reviewed"
        assert status["providers"]["longcat"]["confidence"] == 1.0
        assert status["providers"]["longcat"]["enabled"] is True

        future_observed = (
            datetime.now(timezone.utc) + timedelta(seconds=2)
        ).isoformat().replace("+00:00", "Z")
        run(
            temporary,
            "record-provider-observation",
            "--project",
            str(project),
            "--provider",
            "longcat",
            "--observed-at",
            future_observed,
            "--source",
            "urn:costmarshal:provider-probe:network-timeout",
            "--evidence-sha256",
            "sha256:" + "2" * 64,
            "--api-schema",
            "unknown",
            "--pricing",
            "unknown",
            "--capabilities",
            "unknown",
            "--behavior",
            "unknown",
            "--apply",
            "--command-id",
            "provider-unknown-2",
        )
        degraded = json.loads(
            run(
                temporary,
                "provider-metadata-status",
                "--project",
                str(project),
                "--provider",
                "longcat",
            ).stdout
        )
        assert degraded["providers"]["longcat"]["state"] == "degraded"
        assert degraded["providers"]["longcat"]["confidence"] == 0.5
        effective_longcat = next(
            row
            for row in degraded["effective_catalog"]["providers"]
            if row["provider_id"] == "longcat"
        )
        assert effective_longcat["priority"] == 1100

        duplicate = json.loads(
            run(
                temporary,
                "record-provider-observation",
                "--project",
                str(project),
                "--provider",
                "longcat",
                "--source",
                "urn:costmarshal:ignored-on-replay",
                "--evidence-sha256",
                "sha256:" + "3" * 64,
                "--api-schema",
                "match",
                "--pricing",
                "match",
                "--capabilities",
                "match",
                "--behavior",
                "match",
                "--apply",
                "--command-id",
                "provider-unknown-2",
            ).stdout
        )
        assert duplicate["idempotent_replay"] is True
        print("provider metadata lifecycle contract ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
