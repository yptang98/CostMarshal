#!/usr/bin/env python3
"""Contract tests for the read-only ArchMarshal governance bridge."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import textwrap
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.governance import (  # noqa: E402
    BINDING_FORMAT,
    GovernanceError,
    inspect_governance,
    validate_governance_binding,
)


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
                    "engine_api": "archmarshal-engine-api-v1",
                    "engine_version": "0.14.0",
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
                    "mode": "read_only",
                    "source_mutation": False,
                    "workspace_root": workspace,
                    "state": state,
                    "summary": {{"error": errors, "warning": warnings, "info": len(findings)}},
                    "findings": findings,
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


def inspect_without_workspace_changes(
    workspace: Path,
    wrapper: Path,
    *,
    mode: str,
    state: str,
) -> dict[str, object]:
    before = tree_state(workspace)
    with environment(FAKE_ARCHMARSHAL_STATE=state):
        result = inspect_governance(workspace, mode=mode, wrapper_path=wrapper)
    assert_true(tree_state(workspace) == before, f"{mode}/{state} inspection changed workspace")
    return result


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="costmarshal-archmarshal-contract-") as tmp:
        temp = Path(tmp)
        wrapper = temp / "invoke_archmarshal.py"
        call_log = temp / "wrapper-calls.log"
        write_fake_wrapper(wrapper)

        with environment(FAKE_ARCHMARSHAL_CALL_LOG=str(call_log)):
            off = inspect_governance(temp / "does-not-need-to-exist", mode="off")
            assert_true(off["status"] == "off" and off["binding"] is None, "off must skip governance")
            assert_true(not call_log.exists(), "off must not invoke a wrapper")

            missing_wrapper = inspect_governance(temp, mode="auto", wrapper_path=None)
            assert_true(missing_wrapper["status"] == "warning", "auto must warn without wrapper")
            assert_raises(
                "archmarshal_wrapper_required",
                lambda: inspect_governance(temp, mode="required", wrapper_path=None),
            )

            healthy_workspace = create_owned_workspace(temp / "healthy")
            healthy = inspect_without_workspace_changes(
                healthy_workspace, wrapper, mode="required", state="healthy"
            )
            binding = healthy["binding"]
            assert_true(healthy["ready"] is True and isinstance(binding, dict), "healthy must bind")
            assert_true(binding["format"] == BINDING_FORMAT, "binding format must be stable")
            assert_true(binding["engine_api"] == "archmarshal-engine-api-v1", "engine API missing")
            assert_true(binding["engine_version"] == "0.14.0", "engine version missing")
            assert_true(binding["engine_source_sha256"] == SOURCE_HASH, "engine source hash missing")
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
                    wrapper_path=wrapper,
                )
            assert_true(validated["valid"] is True and validated["drift"] == [], "binding should validate")
            assert_true(tree_state(healthy_workspace) == before_validation, "validation changed workspace")

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
                        wrapper_path=wrapper,
                    ),
                )
            assert_true(absent_error.details["doctor_state"] == "absent", "absent state not retained")
            assert_true(tree_state(absent_workspace) == before_absent_required, "required absent changed workspace")

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
                        wrapper_path=wrapper,
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
                        wrapper_path=wrapper,
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
                    wrapper_path=wrapper,
                )
                assert_raises(
                    "governance_binding_drift",
                    lambda: validate_governance_binding(
                        marker_binding,
                        marker_workspace,
                        mode="required",
                        wrapper_path=wrapper,
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
                    wrapper_path=wrapper,
                )
                assert_raises(
                    "governance_binding_drift",
                    lambda: validate_governance_binding(
                        head_binding,
                        head_workspace,
                        mode="required",
                        wrapper_path=wrapper,
                    ),
                )
            assert_true(head_auto["valid"] is False, "HEAD drift must invalidate")
            assert_true(
                {row["field"] for row in head_auto["drift"]} == {"skill_index_head"},
                "HEAD drift must be precise",
            )
            assert_true(tree_state(head_workspace) == before_head_drift, "HEAD validation changed workspace")

        calls = call_log.read_text(encoding="utf-8").splitlines()
        assert_true(calls, "fake wrapper should have recorded read-only calls")
        assert_true(all(call == "--bootstrap-status" or call.startswith("doctor|") for call in calls), "unexpected ArchMarshal operation")
        forbidden = ("adopt", "start", "end", "apply")
        assert_true(not any(any(word in call for word in forbidden) for call in calls), "mutating lifecycle operation invoked")

    print("archmarshal compatibility ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
