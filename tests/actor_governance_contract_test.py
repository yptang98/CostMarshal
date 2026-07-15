#!/usr/bin/env python3
"""Direct actor entry revalidates ready governance before any side effect."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"
ACTOR = ROOT / "scripts" / "costmarshal_actor.py"
SOURCE_HASH = "a" * 64
SKILL_HEAD = "b" * 64
IMAGE = "ghcr.io/example/costmarshal-worker@sha256:" + ("c" * 64)


def cli(temp: Path, *args: str) -> dict:
    env = os.environ.copy()
    env["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
    completed = subprocess.run(
        [sys.executable, str(CLI), "--root", str(temp / "runtime"), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(f"command failed: {args}\n{completed.stdout}\n{completed.stderr}")
    return json.loads(completed.stdout)


def write_wrapper(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            f"""\
            import json
            import os
            import sys
            from pathlib import Path

            arguments = sys.argv[1:]
            counter_path = os.environ.get("FAKE_ARCHMARSHAL_CALL_COUNTER")
            if counter_path:
                counter = Path(counter_path)
                count = int(counter.read_text(encoding="utf-8").strip()) if counter.exists() else 0
                count += 1
                counter.write_text(str(count), encoding="utf-8")
                drift_at = int(os.environ.get("FAKE_ARCHMARSHAL_DRIFT_AT", "0"))
                marker_path = os.environ.get("FAKE_ARCHMARSHAL_DRIFT_MARKER")
                if marker_path and count == drift_at:
                    marker = Path(marker_path)
                    ownership = json.loads(marker.read_text(encoding="utf-8"))
                    ownership["workspace_id"] = "workspace-drifted-during-actor"
                    marker.write_text(json.dumps(ownership, sort_keys=True) + "\\n", encoding="utf-8")
            if arguments == ["--bootstrap-status"]:
                print(json.dumps({{
                    "api_version": "archmarshal-plugin-bootstrap-v2",
                    "mode": "ready",
                    "verified": True,
                    "engine_api": "archmarshal-engine-api-v1",
                    "engine_version": "0.14.0",
                    "source_tree_sha256": "{SOURCE_HASH}",
                }}))
                raise SystemExit(0)
            if len(arguments) == 2 and arguments[0] == "doctor":
                print(json.dumps({{
                    "api_version": "archmarshal-cli-v1",
                    "mode": "read_only",
                    "source_mutation": False,
                    "workspace_root": str(Path(arguments[1]).resolve()),
                    "state": "healthy",
                    "summary": {{"error": 0, "warning": 0, "info": 0}},
                    "findings": [],
                }}))
                raise SystemExit(0)
            raise SystemExit(7)
            """
        ),
        encoding="utf-8",
    )


def ownership_bytes(workspace_id: str) -> bytes:
    return (
        json.dumps(
            {
                "format": "archmarshal-workspace-ownership-v1",
                "workspace_id": workspace_id,
                "managed_root": ".",
                "skill_index": "required",
                "source_mutation": False,
            },
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted([root, *root.rglob("*")], key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix() or "."
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and not path.is_symlink():
            kind = b"file"
            payload = path.read_bytes()
        elif stat.S_ISDIR(info.st_mode):
            kind = b"directory"
            payload = b""
        elif stat.S_ISLNK(info.st_mode):
            kind = b"symlink"
            payload = os.fsencode(os.readlink(path))
        else:
            kind = b"other"
            payload = b""
        digest.update(os.fsencode(relative))
        digest.update(b"\0" + kind + b"\0")
        digest.update(int(info.st_mode).to_bytes(8, "big", signed=False))
        digest.update(int(info.st_mtime_ns).to_bytes(16, "big", signed=False))
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def tree_manifest(root: Path) -> dict[str, tuple[int, int, str | None]]:
    manifest: dict[str, tuple[int, int, str | None]] = {}
    for path in sorted([root, *root.rglob("*")], key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix() or "."
        info = path.lstat()
        payload_hash = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if stat.S_ISREG(info.st_mode) and not path.is_symlink()
            else None
        )
        manifest[relative] = (int(info.st_mode), int(info.st_mtime_ns), payload_hash)
    return manifest


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-v2-actor-governance-"))
    try:
        workspace = temp / "workspace"
        marker = workspace / ".agent" / "ownership.json"
        head = workspace / ".agent" / "skill-overlays" / ".archmarshal" / "HEAD"
        marker.parent.mkdir(parents=True)
        head.parent.mkdir(parents=True)
        marker.write_bytes(ownership_bytes("workspace-original"))
        head.write_text(SKILL_HEAD + "\n", encoding="ascii")
        wrapper = temp / "invoke_archmarshal.py"
        write_wrapper(wrapper)

        initialized = cli(
            temp,
            "init",
            "--name",
            "actor-governance",
            "--objective",
            "Block direct actor launch after governance drift",
            "--workspace",
            str(workspace),
            "--backend",
            "local",
            "--governance",
            "auto",
            "--archmarshal-wrapper",
            str(wrapper),
            "--allow-unsafe-native-workers",
        )
        project = Path(initialized["project"])
        project_payload = json.loads((project / "project.json").read_text(encoding="utf-8"))
        assert project_payload["governance"]["ready"] is True
        assert project_payload["governance"]["binding"]

        cli(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "governed actor",
            "--purpose",
            "prove the direct entry gate",
            "--risk",
            "low",
        )
        dispatched = cli(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--unsafe-native",
        )
        actor_id = dispatched["actor_id"]
        actor_path = project / "scheduler" / "actors" / f"{actor_id}.json"
        actor = json.loads(
            actor_path.read_text(encoding="utf-8")
        )
        native_isolation = json.loads(json.dumps(actor["isolation"]))

        provider_counter = temp / "provider-calls.txt"
        fake_provider = temp / "fake_provider.py"
        fake_provider.write_text(
            "\n".join(
                [
                    "import pathlib",
                    "import sys",
                    f"pathlib.Path({str(provider_counter)!r}).write_text('called\\n', encoding='utf-8')",
                    "output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])",
                    "output.write_text('# Completion Report\\n\\nStatus: done\\n', encoding='utf-8')",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        command = [
            sys.executable,
            str(ACTOR),
            "--root",
            str(temp / "runtime"),
            "--project",
            str(project),
            "--actor",
            actor_id,
            "--attempt",
            actor["attempt_id"],
            "--launch-token",
            actor["launch_token"],
        ]
        env = os.environ.copy()
        env["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
        env["COSTMARSHAL_CODEX_COMMAND_JSON"] = json.dumps([sys.executable, str(fake_provider)])

        marker.write_bytes(ownership_bytes("workspace-drifted"))
        before = tree_digest(project)
        blocked = subprocess.run(command, text=True, capture_output=True, env=env, check=False)
        after = tree_digest(project)

        assert blocked.returncode != 0, "direct actor entry accepted stale governance"
        error = blocked.stdout + blocked.stderr
        assert "governance gate blocked actor launch" in error.lower(), error
        assert "governance_binding_drift" in error, error
        assert not provider_counter.exists(), "stale governance reached the native provider"
        assert after == before, "governance failure mutated the CostMarshal project"

        # Required OCI entry uses the same gate before bundle/profile/credential
        # preparation.  This fixture intentionally needs no container engine:
        # stale governance must reject it before isolation setup is observable.
        project_payload["governance"]["mode"] = "required"
        (project / "project.json").write_text(
            json.dumps(project_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        actor["isolation"] = {
            "mode": "required",
            "project_opt_in": False,
            "dispatch_opt_in": False,
            "attestation": {
                "schema": "costmarshal-worker-isolation-attestation-v1",
                "backend": "docker",
                "image": IMAGE,
                "image_digest": "sha256:" + ("c" * 64),
                "strong_isolation": True,
            },
            "execution": {
                "engine": "docker",
                "image": IMAGE,
                "network_mode": "provider-proxy",
                "network_name": "costmarshal-provider-proxy",
                "workspace_mode": "ro",
            },
        }
        actor_path.write_text(
            json.dumps(actor, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        required_before = tree_digest(project)
        required_blocked = subprocess.run(
            command,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        assert required_blocked.returncode != 0, "required OCI actor accepted stale governance"
        required_error = required_blocked.stdout + required_blocked.stderr
        assert "governance gate blocked actor launch" in required_error.lower(), required_error
        assert "governance_binding_drift" in required_error, required_error
        assert tree_digest(project) == required_before, "required governance failure mutated the project"
        assert not list((temp / "runtime" / "worker-bundles").rglob("provider.secret"))
        assert not provider_counter.exists(), "required governance reached a provider"

        # The entry gate is repeated immediately before native registration and
        # Popen.  Drift injected by the third wrapper call happens after the
        # first actor-entry inspection, so only the second gate can catch it.
        project_payload["governance"]["mode"] = "auto"
        (project / "project.json").write_text(
            json.dumps(project_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        actor["isolation"] = native_isolation
        actor_path.write_text(
            json.dumps(actor, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        marker.write_bytes(ownership_bytes("workspace-original"))
        wrapper_counter = temp / "actor-wrapper-calls.txt"
        second_gate_env = dict(env)
        second_gate_env.pop("CODEX_HOME", None)
        second_gate_env.update(
            {
                "FAKE_ARCHMARSHAL_CALL_COUNTER": str(wrapper_counter),
                "FAKE_ARCHMARSHAL_DRIFT_AT": "3",
                "FAKE_ARCHMARSHAL_DRIFT_MARKER": str(marker),
            }
        )
        second_gate_before = tree_manifest(project)
        second_gate_blocked = subprocess.run(
            command,
            text=True,
            capture_output=True,
            env=second_gate_env,
            check=False,
        )
        second_gate_error = second_gate_blocked.stdout + second_gate_blocked.stderr
        assert second_gate_blocked.returncode != 0, "native pre-Popen drift reached the provider"
        assert "governance gate blocked actor launch" in second_gate_error.lower(), second_gate_error
        assert "governance_binding_drift" in second_gate_error, second_gate_error
        assert int(wrapper_counter.read_text(encoding="utf-8")) >= 3
        second_gate_after = tree_manifest(project)
        changed_project_paths = sorted(
            path
            for path in set(second_gate_before) | set(second_gate_after)
            if second_gate_before.get(path) != second_gate_after.get(path)
        )
        unexpected_project_paths = [
            path
            for path in changed_project_paths
            if path not in {".", "locks", "locks/attempts"}
            and not path.startswith("locks/attempts/")
        ]
        assert not unexpected_project_paths, (
            "native second gate mutated non-fence project state: "
            + ", ".join(unexpected_project_paths)
        )
        assert not (project / "actor-homes").exists(), "second gate allowed credential-home setup"
        assert not provider_counter.exists(), "native second gate called the provider"
        print("actor governance contract ok")
        return 0
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
