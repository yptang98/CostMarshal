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
import time
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
    env["CODEX_HOME"] = str(temp / "codex-home")
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
    assert path.name == "run_archmarshal.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_name("invoke_archmarshal.py").write_text(
        "# fake canonical invoke wrapper\n",
        encoding="utf-8",
    )
    path.write_text(
        textwrap.dedent(
            f"""\
            import json
            import sys
            from pathlib import Path

            arguments = sys.argv[1:]
            if arguments == ["--bootstrap-status"]:
                print(json.dumps({{
                    "api_version": "archmarshal-plugin-bootstrap-v2",
                    "mode": "ready",
                    "verified": True,
                    "engine_api": "archmarshal-engine-api-v1",
                    "engine_version": "0.15.0",
                    "source_tree_sha256": "{SOURCE_HASH}",
                }}))
                raise SystemExit(0)
            if len(arguments) == 2 and arguments[0] == "doctor":
                print(json.dumps({{
                    "api_version": "archmarshal-cli-v1",
                    "payload_schema_version": "archmarshal-doctor-v1",
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


def wait_for_file(path: Path, process: subprocess.Popen[str], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.is_file():
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"actor exited before creating {path.name}:\n{stdout}\n{stderr}"
            )
        if time.monotonic() >= deadline:
            process.kill()
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"timed out waiting for {path.name}:\n{stdout}\n{stderr}"
            )
        time.sleep(0.01)


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-v2-actor-governance-"))
    previous_codex_home = os.environ.get("CODEX_HOME")
    try:
        codex_home = temp / "codex-home"
        os.environ["CODEX_HOME"] = str(codex_home)
        configured = cli(
            temp,
            "configure-profiles",
            "--codex-home",
            str(codex_home),
        )
        assert configured["profile"] == "longcat"
        assert Path(configured["path"]).is_file()

        workspace = temp / "workspace"
        marker = workspace / ".agent" / "ownership.json"
        head = workspace / ".agent" / "skill-overlays" / ".archmarshal" / "HEAD"
        marker.parent.mkdir(parents=True)
        head.parent.mkdir(parents=True)
        marker.write_bytes(ownership_bytes("workspace-original"))
        head.write_text(SKILL_HEAD + "\n", encoding="ascii")
        wrapper = temp / "run_archmarshal.py"
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
            "--archmarshal-launcher",
            str(wrapper),
            "--allow-unsafe-native-workers",
        )
        project = Path(initialized["project"])
        project_payload = json.loads((project / "project.json").read_text(encoding="utf-8"))
        assert project_payload["governance"]["ready"] is True
        assert project_payload["governance"]["binding"]
        ready_governance = json.loads(json.dumps(project_payload["governance"]))

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
        before_auto_ready_dispatch = tree_digest(project)
        dispatch_environment = os.environ.copy()
        dispatch_environment["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
        dispatch_environment["CODEX_HOME"] = str(codex_home)
        rejected_auto_ready = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--root",
                str(temp / "runtime"),
                "dispatch",
                "--project",
                str(project),
                "--task",
                "V2-0001",
                "--unsafe-native",
            ],
            text=True,
            capture_output=True,
            env=dispatch_environment,
            check=False,
        )
        assert rejected_auto_ready.returncode != 0
        assert "active governance forbids unsafe-native" in (
            rejected_auto_ready.stdout + rejected_auto_ready.stderr
        )
        assert tree_digest(project) == before_auto_ready_dispatch

        # Build a direct-entry fixture under governance off, then restore the
        # original ready binding.  New native attempts are forbidden; this
        # pre-existing fixture is used only to prove actor-entry drift gates.
        project_payload["governance"] = {
            "mode": "off",
            "ready": False,
            "status": "off",
            "binding": None,
        }
        (project / "project.json").write_text(
            json.dumps(project_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
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
        project_payload["governance"] = ready_governance
        (project / "project.json").write_text(
            json.dumps(project_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        actor_id = dispatched["actor_id"]
        actor_path = project / "scheduler" / "actors" / f"{actor_id}.json"
        actor = json.loads(
            actor_path.read_text(encoding="utf-8")
        )
        native_isolation = json.loads(json.dumps(actor["isolation"]))

        provider_counter = temp / "provider-calls.txt"
        provider_started = temp / "provider-started.pid"
        fake_provider = temp / "fake_provider.py"
        fake_provider.write_text(
            "\n".join(
                [
                    "import os",
                    "import pathlib",
                    "import sys",
                    f"pathlib.Path({str(provider_started)!r}).write_text(str(os.getpid()), encoding='ascii')",
                    "prompt = sys.stdin.read()",
                    "if not prompt: raise SystemExit(0)",
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
        env["CODEX_HOME"] = str(codex_home)
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

        # Native execution is permitted only while governance is off. Pause
        # after Popen but before post-spawn authorization, then make the
        # project governed and drift ownership. The child counts a provider
        # call only after receiving its prompt, so zero calls verifies the
        # stdin authorization fence across process creation.
        project_payload["governance"] = {
            **ready_governance,
            "mode": "off",
            "ready": False,
            "status": "off",
        }
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
        barrier_ready = temp / "native-launch.ready"
        barrier_release = temp / "native-launch.release"
        barrier_env = dict(env)
        # The launch path must consume only the immutable admitted snapshot;
        # it must not re-resolve the named source profile.
        barrier_env.pop("CODEX_HOME", None)
        barrier_env.update(
            {
                "COSTMARSHAL_NATIVE_LAUNCH_BARRIER_STAGE": (
                    "after_popen_before_governance"
                ),
                "COSTMARSHAL_NATIVE_LAUNCH_BARRIER_READY": str(barrier_ready),
                "COSTMARSHAL_NATIVE_LAUNCH_BARRIER_RELEASE": str(barrier_release),
            }
        )
        barrier_process = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=barrier_env,
        )
        wait_for_file(barrier_ready, barrier_process)
        wait_for_file(provider_started, barrier_process)
        project_payload["governance"] = ready_governance
        (project / "project.json").write_text(
            json.dumps(project_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        marker.write_bytes(ownership_bytes("workspace-drifted-during-launch"))
        barrier_release.write_text("release\n", encoding="ascii")
        try:
            barrier_stdout, barrier_stderr = barrier_process.communicate(timeout=20.0)
        except subprocess.TimeoutExpired:
            barrier_process.kill()
            barrier_stdout, barrier_stderr = barrier_process.communicate()
            raise AssertionError(
                "native governance barrier actor did not exit:\n"
                + barrier_stdout
                + "\n"
                + barrier_stderr
            )
        barrier_error = barrier_stdout + barrier_stderr
        assert barrier_process.returncode != 0, "post-Popen governance drift reached provider"
        assert "governance gate blocked actor launch" in barrier_error.lower(), barrier_error
        assert "governance_binding_drift" in barrier_error, barrier_error
        assert not provider_counter.exists(), "governance drift delivered the provider prompt"
        blocked_actor = json.loads(actor_path.read_text(encoding="utf-8"))
        blocked_task = json.loads(
            (project / "tasks" / "V2-0001" / "task.json").read_text(encoding="utf-8")
        )
        assert blocked_actor["status"] == "needs_recovery"
        blocked_runtime = blocked_actor["runtime"]
        assert (
            blocked_runtime["provider_execution_state"]
            == "not_started_governance_blocked"
        )
        assert blocked_runtime["governance_launch_block"]["stage"] == "after_popen"
        assert blocked_runtime["governance_launch_block"]["child_terminated"] is True
        assert blocked_task["status"] == "needs_recovery"
        assert blocked_task["attempts"][-1]["status"] == "needs_recovery"
        assert (
            blocked_task["attempts"][-1]["provider_execution_state"]
            == "not_started_governance_blocked"
        )
        print("actor governance contract ok")
        return 0
    finally:
        if previous_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = previous_codex_home
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
