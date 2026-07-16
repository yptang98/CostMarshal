#!/usr/bin/env python3
"""Contract tests for the read-only ArchMarshal governance bridge."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"
sys.path.insert(0, str(ROOT))

from costmarshal_v2.governance import (  # noqa: E402
    BINDING_FORMAT,
    GovernanceError,
    enforce_governance_contract,
    inspect_governance,
    validate_governance_binding,
)
from costmarshal_v2.control_store import control_transaction  # noqa: E402
from costmarshal_v2.paths import ProjectLayout  # noqa: E402
from costmarshal_v2.scheduler import (  # noqa: E402
    GovernancePreflightBlocked,
    process_runtime_effects,
    scheduler_cycle,
)
from costmarshal_v2.state import load_project, save_project  # noqa: E402


SOURCE_HASH = "a" * 64
INITIAL_HEAD = "b" * 64


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def assert_raises(code: str, callback: object) -> GovernanceError:
    try:
        callback()  # type: ignore[operator]
    except GovernanceError as exc:
        assert_true(exc.code == code, f"expected {code}, got {exc.code}")
        return exc
    raise AssertionError(f"expected GovernanceError({code})")


@contextmanager
def environment(**updates: str | None) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def write_fake_wrapper(path: Path) -> None:
    assert path.name == "run_archmarshal.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_name("invoke_archmarshal.py").write_text(
        "# fake canonical invoke wrapper; identity is bound by CostMarshal\n",
        encoding="utf-8",
    )
    path.write_text(
        textwrap.dedent(
            f"""\
            import json
            import os
            import sys
            from pathlib import Path

            arguments = sys.argv[1:]
            log_path = os.environ.get("FAKE_ARCHMARSHAL_CALL_LOG")
            if log_path:
                with Path(log_path).open("a", encoding="utf-8") as handle:
                    handle.write("|".join(arguments) + "\\n")

            if arguments == ["--bootstrap-status"]:
                print(json.dumps({{
                    "api_version": "archmarshal-plugin-bootstrap-v2",
                    "mode": "ready",
                    "verified": True,
                    "engine_api": os.environ.get("FAKE_ARCHMARSHAL_ENGINE_API", "archmarshal-engine-api-v1"),
                    "engine_version": os.environ.get("FAKE_ARCHMARSHAL_ENGINE_VERSION", "0.15.0"),
                    "source_tree_sha256": "{SOURCE_HASH}",
                    "source": "SHOULD_NOT_APPEAR_IN_BINDING",
                }}))
                raise SystemExit(0)

            if len(arguments) == 2 and arguments[0] == "doctor":
                state = os.environ.get("FAKE_ARCHMARSHAL_STATE", "healthy")
                workspace = str(Path(arguments[1]).resolve())
                findings = []
                errors = 0
                warnings = 0
                if state == "absent":
                    findings = [{{"classification": "absent", "code": "workspace_unowned"}}]
                elif state == "corrupt":
                    findings = [{{"classification": "corrupt", "code": "ownership_json_corrupt"}}]
                    errors = 1
                    state = "error"
                elif state == "partial":
                    findings = [{{"classification": "partial", "code": "session_uncommitted"}}]
                    warnings = 1
                    state = "warning"
                print(json.dumps({{
                    "api_version": "archmarshal-cli-v1",
                    "payload_schema_version": os.environ.get("FAKE_ARCHMARSHAL_DOCTOR_SCHEMA", "archmarshal-doctor-v1"),
                    "mode": "read_only",
                    "source_mutation": False,
                    "workspace_root": workspace,
                    "state": state,
                    "summary": (
                        {{"error": True, "warning": warnings, "info": len(findings)}}
                        if os.environ.get("FAKE_ARCHMARSHAL_BAD_SUMMARY")
                        else {{"error": errors, "warning": warnings, "info": len(findings)}}
                    ),
                    "findings": "invalid" if os.environ.get("FAKE_ARCHMARSHAL_BAD_FINDINGS") else findings,
                }}))
                raise SystemExit(0)

            print(json.dumps({{"error": "unsupported fake operation", "arguments": arguments}}))
            raise SystemExit(7)
            """
        ),
        encoding="utf-8",
    )


def ownership_bytes(workspace_id: str = "workspace-test") -> bytes:
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


def create_owned_workspace(root: Path) -> Path:
    workspace = root / "workspace"
    marker = workspace / ".agent" / "ownership.json"
    head = workspace / ".agent" / "skill-overlays" / ".archmarshal" / "HEAD"
    marker.parent.mkdir(parents=True)
    head.parent.mkdir(parents=True)
    marker.write_bytes(ownership_bytes())
    head.write_text(INITIAL_HEAD + "\n", encoding="ascii")
    (workspace / "sentinel.txt").write_text("must remain byte-identical\n", encoding="utf-8")
    return workspace


def tree_state(root: Path) -> dict[str, tuple[int, int, bytes | None]]:
    state: dict[str, tuple[int, int, bytes | None]] = {}
    paths = sorted([root, *root.rglob("*")], key=lambda item: str(item))
    for path in paths:
        metadata = path.lstat()
        content = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        state[path.relative_to(root).as_posix() or "."] = (
            stat.S_IFMT(metadata.st_mode) | stat.S_IMODE(metadata.st_mode),
            metadata.st_mtime_ns,
            content,
        )
    return state


def tree_digest(root: Path) -> str:
    """Hash every project path and byte payload, including DB/WAL/SHM and views."""

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
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def inspect_without_workspace_changes(
    workspace: Path,
    wrapper: Path,
    *,
    mode: str,
    state: str,
) -> dict[str, object]:
    before = tree_state(workspace)
    with environment(FAKE_ARCHMARSHAL_STATE=state):
        result = inspect_governance(workspace, mode=mode, launcher_path=wrapper)
    assert_true(tree_state(workspace) == before, f"{mode}/{state} inspection changed workspace")
    return result


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="costmarshal-archmarshal-contract-") as tmp:
        temp = Path(tmp)
        wrapper = temp / "run_archmarshal.py"
        call_log = temp / "wrapper-calls.log"
        write_fake_wrapper(wrapper)

        with environment(FAKE_ARCHMARSHAL_CALL_LOG=str(call_log)):
            off = inspect_governance(temp / "does-not-need-to-exist", mode="off")
            assert_true(off["status"] == "off" and off["binding"] is None, "off must skip governance")
            assert_true(not call_log.exists(), "off must not invoke a wrapper")

            missing_wrapper = inspect_governance(temp, mode="auto", launcher_path=None)
            assert_true(missing_wrapper["status"] == "warning", "auto must warn without wrapper")
            assert_raises(
                "archmarshal_launcher_required",
                lambda: inspect_governance(temp, mode="required", launcher_path=None),
            )
            assert_raises(
                "archmarshal_canonical_launcher_required",
                lambda: inspect_governance(
                    temp,
                    mode="required",
                    launcher_path=wrapper.with_name("invoke_archmarshal.py"),
                ),
            )
            with environment(FAKE_ARCHMARSHAL_ENGINE_API="unknown-engine-api"):
                assert_raises(
                    "archmarshal_bootstrap_unverified",
                    lambda: inspect_governance(temp, mode="required", launcher_path=wrapper),
                )
            with environment(FAKE_ARCHMARSHAL_ENGINE_VERSION="0.14.0"):
                assert_raises(
                    "archmarshal_bootstrap_unverified",
                    lambda: inspect_governance(temp, mode="required", launcher_path=wrapper),
                )

            healthy_workspace = create_owned_workspace(temp / "healthy")
            healthy = inspect_without_workspace_changes(
                healthy_workspace, wrapper, mode="required", state="healthy"
            )
            binding = healthy["binding"]
            assert_true(healthy["ready"] is True and isinstance(binding, dict), "healthy must bind")
            assert_true(binding["format"] == BINDING_FORMAT, "binding format must be stable")
            assert_true(binding["engine_api"] == "archmarshal-engine-api-v1", "engine API missing")
            assert_true(binding["engine_version"] == "0.15.0", "engine version missing")
            assert_true(binding["engine_source_sha256"] == SOURCE_HASH, "engine source hash missing")
            assert_true(binding["launcher_sha256"], "launcher hash missing")
            assert_true(binding["launcher_size"] == wrapper.stat().st_size, "launcher size missing")
            assert_true(binding["invoke_wrapper_sha256"], "invoke wrapper hash missing")
            assert_true(
                binding["invoke_wrapper_size"] == wrapper.with_name("invoke_archmarshal.py").stat().st_size,
                "invoke wrapper size missing",
            )
            assert_true(binding["doctor_api_version"] == "archmarshal-cli-v1", "doctor API missing")
            assert_true(
                binding["doctor_payload_schema_version"] == "archmarshal-doctor-v1",
                "doctor schema missing",
            )
            assert_true(binding["skill_index_head"] == INITIAL_HEAD, "Skill HEAD missing")
            assert_true(binding["ownership_marker_sha256"], "ownership marker hash missing")
            assert_true(binding["workspace_root"] == str(healthy_workspace.resolve()), "root mismatch")
            serialized_binding = json.dumps(binding, sort_keys=True)
            assert_true("SHOULD_NOT_APPEAR" not in serialized_binding, "binding leaked engine path")
            assert_true("findings" not in serialized_binding and "workspace_id" not in serialized_binding, "binding leaked raw metadata")

            before_validation = tree_state(healthy_workspace)
            with environment(FAKE_ARCHMARSHAL_STATE="healthy"):
                validated = validate_governance_binding(
                    binding,
                    healthy_workspace,
                    mode="required",
                    launcher_path=wrapper,
                )
            assert_true(validated["valid"] is True and validated["drift"] == [], "binding should validate")
            assert_true(tree_state(healthy_workspace) == before_validation, "validation changed workspace")

            for variable, expected_issue in (
                ("FAKE_ARCHMARSHAL_DOCTOR_SCHEMA", "archmarshal_doctor_schema_invalid"),
                ("FAKE_ARCHMARSHAL_BAD_SUMMARY", "archmarshal_doctor_summary_invalid"),
                ("FAKE_ARCHMARSHAL_BAD_FINDINGS", "archmarshal_doctor_findings_invalid"),
            ):
                value = "wrong-doctor-schema" if variable.endswith("SCHEMA") else "1"
                with environment(**{variable: value}):
                    error = assert_raises(
                        "archmarshal_governance_not_ready",
                        lambda: inspect_governance(
                            healthy_workspace,
                            mode="required",
                            launcher_path=wrapper,
                        ),
                    )
                assert_true(
                    expected_issue in error.details["issue_codes"],
                    f"{variable} did not fail closed",
                )

            absent_workspace = temp / "absent" / "workspace"
            absent_workspace.mkdir(parents=True)
            (absent_workspace / "sentinel.txt").write_text("absent\n", encoding="utf-8")
            absent = inspect_without_workspace_changes(
                absent_workspace, wrapper, mode="auto", state="absent"
            )
            assert_true(absent["status"] == "warning" and absent["ready"] is False, "auto absent must warn")
            before_absent_required = tree_state(absent_workspace)
            with environment(FAKE_ARCHMARSHAL_STATE="absent"):
                absent_error = assert_raises(
                    "archmarshal_governance_not_ready",
                    lambda: inspect_governance(
                        absent_workspace,
                        mode="required",
                    launcher_path=wrapper,
                    ),
                )
            assert_true(absent_error.details["doctor_state"] == "absent", "absent state not retained")
            assert_true(tree_state(absent_workspace) == before_absent_required, "required absent changed workspace")
            auto_governance = {
                "mode": "auto",
                "ready": False,
                "launcher_path": str(wrapper),
                "binding": absent.get("binding"),
            }
            with environment(FAKE_ARCHMARSHAL_STATE="absent"):
                absent_contract = enforce_governance_contract(
                    auto_governance,
                    absent_workspace,
                    operation="test absent rediscovery",
                )
            assert_true(absent_contract["governed"] is False, "explicit absence must remain usable")
            marker = absent_workspace / ".agent" / "ownership.json"
            head = absent_workspace / ".agent" / "skill-overlays" / ".archmarshal" / "HEAD"
            marker.parent.mkdir(parents=True, exist_ok=True)
            head.parent.mkdir(parents=True, exist_ok=True)
            marker.write_bytes(ownership_bytes("adopted-after-init"))
            head.write_text(INITIAL_HEAD + "\n", encoding="ascii")
            adopted_before = tree_state(absent_workspace)
            with environment(FAKE_ARCHMARSHAL_STATE="healthy"):
                assert_raises(
                    "governance_rebind_required",
                    lambda: enforce_governance_contract(
                        auto_governance,
                        absent_workspace,
                        operation="test post-init adoption",
                    ),
                )
            assert_true(tree_state(absent_workspace) == adopted_before, "rediscovery mutated workspace")
            no_launcher = {"mode": "auto", "ready": False, "launcher_path": None}
            assert_raises(
                "archmarshal_launcher_required_for_detected_governance",
                lambda: enforce_governance_contract(
                    no_launcher,
                    absent_workspace,
                    operation="test detected governance",
                ),
            )

            corrupt_workspace = create_owned_workspace(temp / "corrupt")
            (corrupt_workspace / ".agent" / "ownership.json").write_text("{not-json\n", encoding="utf-8")
            corrupt = inspect_without_workspace_changes(
                corrupt_workspace, wrapper, mode="auto", state="corrupt"
            )
            corrupt_codes = {item["code"] for item in corrupt["warnings"]}
            assert_true("archmarshal_ownership_invalid" in corrupt_codes, "corrupt marker must warn")
            before_corrupt_required = tree_state(corrupt_workspace)
            with environment(FAKE_ARCHMARSHAL_STATE="corrupt"):
                assert_raises(
                    "archmarshal_governance_not_ready",
                    lambda: inspect_governance(
                        corrupt_workspace,
                        mode="required",
                    launcher_path=wrapper,
                    ),
                )
            assert_true(tree_state(corrupt_workspace) == before_corrupt_required, "corrupt check changed workspace")

            partial_workspace = create_owned_workspace(temp / "partial")
            partial = inspect_without_workspace_changes(
                partial_workspace, wrapper, mode="auto", state="partial"
            )
            partial_codes = {item["code"] for item in partial["warnings"]}
            assert_true("archmarshal_doctor_blocking_state" in partial_codes, "partial must warn")
            before_partial_required = tree_state(partial_workspace)
            with environment(FAKE_ARCHMARSHAL_STATE="partial"):
                assert_raises(
                    "archmarshal_governance_not_ready",
                    lambda: inspect_governance(
                        partial_workspace,
                        mode="required",
                    launcher_path=wrapper,
                    ),
                )
            assert_true(tree_state(partial_workspace) == before_partial_required, "partial check changed workspace")

            marker_workspace = create_owned_workspace(temp / "marker-drift")
            marker_binding = inspect_without_workspace_changes(
                marker_workspace, wrapper, mode="required", state="healthy"
            )["binding"]
            (marker_workspace / ".agent" / "ownership.json").write_bytes(ownership_bytes("changed"))
            before_marker_drift = tree_state(marker_workspace)
            with environment(FAKE_ARCHMARSHAL_STATE="healthy"):
                marker_auto = validate_governance_binding(
                    marker_binding,
                    marker_workspace,
                    mode="auto",
                    launcher_path=wrapper,
                )
                assert_raises(
                    "governance_binding_drift",
                    lambda: validate_governance_binding(
                        marker_binding,
                        marker_workspace,
                        mode="required",
                    launcher_path=wrapper,
                    ),
                )
            assert_true(marker_auto["valid"] is False, "marker drift must invalidate")
            assert_true(
                {row["field"] for row in marker_auto["drift"]} == {"ownership_marker_sha256"},
                "marker drift must be precise",
            )
            assert_true(tree_state(marker_workspace) == before_marker_drift, "marker validation changed workspace")

            head_workspace = create_owned_workspace(temp / "head-drift")
            head_binding = inspect_without_workspace_changes(
                head_workspace, wrapper, mode="required", state="healthy"
            )["binding"]
            (head_workspace / ".agent" / "skill-overlays" / ".archmarshal" / "HEAD").write_text(
                "c" * 64 + "\n", encoding="ascii"
            )
            before_head_drift = tree_state(head_workspace)
            with environment(FAKE_ARCHMARSHAL_STATE="healthy"):
                head_auto = validate_governance_binding(
                    head_binding,
                    head_workspace,
                    mode="auto",
                    launcher_path=wrapper,
                )
                assert_raises(
                    "governance_binding_drift",
                    lambda: validate_governance_binding(
                        head_binding,
                        head_workspace,
                        mode="required",
                    launcher_path=wrapper,
                    ),
                )
            assert_true(head_auto["valid"] is False, "HEAD drift must invalidate")
            assert_true(
                {row["field"] for row in head_auto["drift"]} == {"skill_index_head"},
                "HEAD drift must be precise",
            )
            assert_true(tree_state(head_workspace) == before_head_drift, "HEAD validation changed workspace")

            invoke_workspace = create_owned_workspace(temp / "invoke-drift")
            invoke_binding = inspect_without_workspace_changes(
                invoke_workspace, wrapper, mode="required", state="healthy"
            )["binding"]
            invoke_wrapper = wrapper.with_name("invoke_archmarshal.py")
            invoke_wrapper.write_text(
                invoke_wrapper.read_text(encoding="utf-8") + "# invoke drift\n",
                encoding="utf-8",
            )
            with environment(FAKE_ARCHMARSHAL_STATE="healthy"):
                invoke_auto = validate_governance_binding(
                    invoke_binding,
                    invoke_workspace,
                    mode="auto",
                    launcher_path=wrapper,
                )
            assert_true(invoke_auto["valid"] is False, "invoke drift must invalidate")
            assert_true(
                {row["field"] for row in invoke_auto["drift"]}
                == {"invoke_wrapper_sha256", "invoke_wrapper_size"},
                "invoke drift must be byte-bound",
            )
            # Restore the fake pair before testing launcher-only drift.
            write_fake_wrapper(wrapper)
            wrapper.write_text(
                wrapper.read_text(encoding="utf-8") + "\n# reviewed launcher drift\n",
                encoding="utf-8",
            )
            with environment(FAKE_ARCHMARSHAL_STATE="healthy"):
                wrapper_auto = validate_governance_binding(
                    binding,
                    healthy_workspace,
                    mode="auto",
                    launcher_path=wrapper,
                )
                assert_raises(
                    "governance_binding_drift",
                    lambda: validate_governance_binding(
                        binding,
                        healthy_workspace,
                        mode="required",
                    launcher_path=wrapper,
                    ),
                )
            assert_true(wrapper_auto["valid"] is False, "launcher drift must invalidate")
            assert_true(
                {row["field"] for row in wrapper_auto["drift"]}
                == {"launcher_sha256", "launcher_size"},
                "launcher drift must be byte-bound",
            )

            rebind_wrapper = temp / "rebind" / "run_archmarshal.py"
            write_fake_wrapper(rebind_wrapper)
            runtime = temp / "runtime"
            command = [
                sys.executable,
                str(CLI),
                "--root",
                str(runtime),
                "init",
                "--name",
                "governance-rebind",
                "--objective",
                "explicitly refresh CostMarshal binding",
                "--workspace",
                str(healthy_workspace),
                "--backend",
                "local",
                "--governance",
                "required",
                "--archmarshal-launcher",
                str(rebind_wrapper),
                "--allow-unsafe-native-workers",
            ]
            created = subprocess.run(
                command,
                env={**os.environ, "FAKE_ARCHMARSHAL_STATE": "healthy"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            assert_true(created.returncode == 0, created.stderr)
            project_dir = Path(json.loads(created.stdout)["project"])
            project_path = project_dir / "project.json"
            project_payload = json.loads(project_path.read_text(encoding="utf-8"))
            project_payload["governance"]["binding"]["format"] = "costmarshal-archmarshal-binding-v1"
            project_payload["governance"]["binding"].pop("launcher_sha256", None)
            project_payload["governance"]["binding"].pop("launcher_size", None)
            project_path.write_text(json.dumps(project_payload, indent=2) + "\n", encoding="utf-8")
            stale_bytes = project_path.read_bytes()

            recover = subprocess.run(
                [*command[:4], "recover", "--project", str(project_dir)],
                env={**os.environ, "FAKE_ARCHMARSHAL_STATE": "healthy"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            assert_true(recover.returncode != 0, "required recovery accepted a stale binding")
            assert_true(project_path.read_bytes() == stale_bytes, "blocked recovery changed project state")

            preview = subprocess.run(
                [*command[:4], "governance-rebind", "--project", str(project_dir)],
                env={**os.environ, "FAKE_ARCHMARSHAL_STATE": "healthy"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            assert_true(preview.returncode == 0, preview.stderr)
            assert_true(json.loads(preview.stdout)["mode"] == "preview", "rebind preview missing")
            assert_true(project_path.read_bytes() == stale_bytes, "rebind preview changed project state")
            applied = subprocess.run(
                [
                    *command[:4],
                    "governance-rebind",
                    "--project",
                    str(project_dir),
                    "--apply",
                    "--command-id",
                    "CMD-GOVERNANCE-REBIND",
                ],
                env={**os.environ, "FAKE_ARCHMARSHAL_STATE": "healthy"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            assert_true(applied.returncode == 0, applied.stderr)
            rebound = json.loads(project_path.read_text(encoding="utf-8"))["governance"]
            assert_true(rebound["binding"]["format"] == BINDING_FORMAT, "binding was not upgraded")
            assert_true(rebound["binding_history"][-1]["binding"]["format"].endswith("-v1"), "old binding was not retained")

            # Build one real pending spawn effect while governance is explicitly
            # off, then restore the reviewed required binding transactionally.
            # This lets the drift test prove that run-scheduler cannot lease or
            # execute an already-committed provider effect.
            required_governance = json.loads(json.dumps(rebound))
            project_payload = json.loads(project_path.read_text(encoding="utf-8"))
            project_payload["governance"] = {
                "mode": "off",
                "status": "off",
                "ready": False,
                "binding": None,
                "launcher_path": None,
            }
            project_path.write_text(json.dumps(project_payload, indent=2) + "\n", encoding="utf-8")
            fixture_task = subprocess.run(
                [
                    *command[:4],
                    "new-task",
                    "--project",
                    str(project_dir),
                    "--title",
                    "pending governed spawn",
                    "--purpose",
                    "prove governance blocks an existing provider effect",
                    "--provider",
                    "codex",
                ],
                env={**os.environ, "FAKE_ARCHMARSHAL_STATE": "healthy"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            assert_true(fixture_task.returncode == 0, fixture_task.stderr)
            migrated = subprocess.run(
                [*command[:4], "migrate-state", "--project", str(project_dir), "--apply"],
                env={**os.environ, "FAKE_ARCHMARSHAL_STATE": "healthy"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            assert_true(migrated.returncode == 0, migrated.stderr)
            queued = subprocess.run(
                [
                    *command[:4],
                    "dispatch",
                    "--project",
                    str(project_dir),
                    "--task",
                    "V2-0001",
                    "--provider",
                    "codex",
                    "--unsafe-native",
                    "--start",
                    "--command-id",
                    "CMD-GOVERNANCE-PENDING-SPAWN",
                ],
                env={**os.environ, "FAKE_ARCHMARSHAL_STATE": "healthy"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            assert_true(queued.returncode == 0, queued.stderr)
            queued_payload = json.loads(queued.stdout)
            assert_true(
                queued_payload["started"] is False and queued_payload["start_queued"] is True,
                "fixture dispatch did not leave a pending spawn effect",
            )

            layout = ProjectLayout(root=runtime.resolve(), project_dir=project_dir.resolve())
            with control_transaction(
                layout,
                command_name="test_restore_required_governance",
                command_id="TEST-RESTORE-REQUIRED-GOVERNANCE",
                payload={"project": str(project_dir)},
            ) as transaction:
                assert_true(not transaction.replay, "governance fixture transaction replayed")
                authoritative_project = load_project(layout)
                authoritative_project["governance"] = required_governance
                save_project(layout, authoritative_project)

            # Commit a second command and crash after commit but before view
            # materialization. A rejected command must not reconcile that dirty
            # view, acknowledge dirty_views, lease the spawn, or touch locks.
            dirty_commit = subprocess.run(
                [
                    *command[:4],
                    "new-task",
                    "--project",
                    str(project_dir),
                    "--title",
                    "dirty committed view",
                    "--purpose",
                    "leave an after-commit-before-materialize state",
                    "--command-id",
                    "CMD-GOVERNANCE-DIRTY-VIEW",
                ],
                env={
                    **os.environ,
                    "FAKE_ARCHMARSHAL_STATE": "healthy",
                    "COSTMARSHAL_CONTROL_STORE_FAULT": "transaction.after_commit_before_materialize",
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            assert_true(dirty_commit.returncode == 86, "fixture did not stop at the dirty-view fault")

            fake_provider = temp / "must-not-call-provider.py"
            provider_counter = temp / "governance-provider-calls.txt"
            fake_provider.write_text(
                "from pathlib import Path\n"
                f"Path({str(provider_counter)!r}).write_text('called\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            (healthy_workspace / ".agent" / "skill-overlays" / ".archmarshal" / "HEAD").write_text(
                "d" * 64 + "\n",
                encoding="ascii",
            )
            blocked_environment = {
                **os.environ,
                "FAKE_ARCHMARSHAL_STATE": "healthy",
                "COSTMARSHAL_CODEX_COMMAND_JSON": json.dumps([sys.executable, str(fake_provider)]),
            }
            unchanged_digest = tree_digest(project_dir)

            blocked_scheduler = subprocess.run(
                [*command[:4], "run-scheduler", "--project", str(project_dir), "--once"],
                env=blocked_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            assert_true(blocked_scheduler.returncode != 0, "run-scheduler accepted stale governance")
            assert_true("governance gate blocked" in blocked_scheduler.stderr.lower(), blocked_scheduler.stderr)
            assert_true(tree_digest(project_dir) == unchanged_digest, "blocked scheduler changed project bytes")
            assert_true(not provider_counter.exists(), "blocked scheduler called the provider")

            blocked_recover = subprocess.run(
                [*command[:4], "recover", "--project", str(project_dir)],
                env=blocked_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            assert_true(blocked_recover.returncode != 0, "required recovery accepted stale governance")
            assert_true(tree_digest(project_dir) == unchanged_digest, "blocked recovery changed project bytes")

            blocked_task = subprocess.run(
                [
                    *command[:4],
                    "new-task",
                    "--project",
                    str(project_dir),
                    "--title",
                    "must be rejected",
                    "--purpose",
                    "generic governed mutation",
                ],
                env=blocked_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            assert_true(blocked_task.returncode != 0, "generic governed command accepted stale governance")
            assert_true(tree_digest(project_dir) == unchanged_digest, "blocked generic command changed project bytes")
            assert_true(not provider_counter.exists(), "a blocked governance path called the provider")

            blocked_relay = subprocess.run(
                [
                    *command[:4],
                    "relay",
                    "--project",
                    str(project_dir),
                    "--actor",
                    "leader",
                    "--dry-run",
                ],
                env=blocked_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            assert_true(blocked_relay.returncode != 0, "relay accepted stale governance")
            assert_true(tree_digest(project_dir) == unchanged_digest, "blocked relay changed project bytes")

            blocked_runtime_send = subprocess.run(
                [
                    *command[:4],
                    "send",
                    "--project",
                    str(project_dir),
                    "--to",
                    "leader",
                    "--message",
                    "must not reach the runtime",
                    "--runtime-send",
                ],
                env=blocked_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            assert_true(blocked_runtime_send.returncode != 0, "runtime send accepted stale governance")
            assert_true(
                tree_digest(project_dir) == unchanged_digest,
                "blocked runtime send changed project bytes",
            )

            with environment(FAKE_ARCHMARSHAL_STATE="healthy"):
                for label, callback in (
                    ("scheduler cycle", lambda: scheduler_cycle(layout)),
                    ("runtime effect worker", lambda: process_runtime_effects(layout)),
                ):
                    try:
                        callback()
                    except GovernancePreflightBlocked:
                        pass
                    else:
                        raise AssertionError(f"{label} accepted stale governance")
                    assert_true(
                        tree_digest(project_dir) == unchanged_digest,
                        f"blocked {label} changed project bytes",
                    )
            assert_true(not provider_counter.exists(), "a direct scheduler entry called the provider")

        calls = call_log.read_text(encoding="utf-8").splitlines()
        assert_true(calls, "fake wrapper should have recorded read-only calls")
        assert_true(all(call == "--bootstrap-status" or call.startswith("doctor|") for call in calls), "unexpected ArchMarshal operation")
        forbidden = ("adopt", "start", "end", "apply")
        assert_true(not any(any(word in call for word in forbidden) for call in calls), "mutating lifecycle operation invoked")

    print("archmarshal compatibility ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
