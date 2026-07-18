#!/usr/bin/env python3
"""Fast unit checks for CostMarshal v2 pure helpers."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.paths import ProjectLayout, actor_runtime_name, actor_target, relpath, slugify  # noqa: E402
from costmarshal_v2.actor_runner import (  # noqa: E402
    _resolve_windows_codex_shim,
    _runner_process_start_marker,
    _validate_actor_governance_before_side_effect,
    process_argv,
)
from costmarshal_v2.scheduler import (  # noqa: E402
    _spawn_effect_payload,
    _stop_effect_payload,
    _validate_spawn_observation,
    _validate_stop_effect,
    STOP_EFFECT_TYPE,
    default_backend_session_name,
    normalize_claim_path,
    paths_conflict,
    summarize_leader_self_work,
    summarize_results,
    validate_actor_runtime_binding,
    validate_actor_runtime_authority,
)
from costmarshal_v2.session_backend import (  # noqa: E402
    select_backend_kind,
    tmux_actor_target,
    tmux_format_literal,
    tmux_new_window_target,
    tmux_session_target,
    validate_persisted_tmux_actor_name,
    validate_persisted_tmux_session_name,
    validate_persisted_tmux_target,
    validate_tmux_name,
    validate_tmux_target,
)
import costmarshal_v2.state as state_module  # noqa: E402
from costmarshal_v2.state import can_transition_task, read_json, read_jsonl  # noqa: E402
from costmarshal_v2.session_backend import command_to_string, format_actor_command  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    class SharingConflictPath:
        def __init__(self, content: str, failures: int) -> None:
            self.content = content
            self.failures = failures
            self.reads = 0

        def read_text(self, *, encoding: str) -> str:
            assert encoding == "utf-8"
            self.reads += 1
            if self.reads <= self.failures:
                raise PermissionError("simulated transient Windows sharing conflict")
            return self.content

    json_path = SharingConflictPath('{"status": "ok"}\n', failures=2)
    jsonl_path = SharingConflictPath('{"row": 1}\n{"row": 2}\n', failures=1)
    with patch.object(state_module, "os", SimpleNamespace(name="nt")), patch.object(
        state_module.time,
        "sleep",
    ):
        assert_true(read_json(json_path) == {"status": "ok"}, "JSON sharing retry failed")
        assert_true(json_path.reads == 3, "JSON sharing retry was not bounded to success")
        assert_true(
            read_jsonl(jsonl_path) == [{"row": 1}, {"row": 2}],
            "JSONL sharing retry failed",
        )
        assert_true(jsonl_path.reads == 2, "JSONL sharing retry was not exercised")

    denied_path = SharingConflictPath('{"status": "hidden"}\n', failures=2)
    with patch.object(state_module, "os", SimpleNamespace(name="nt")), patch.object(
        state_module,
        "WINDOWS_READ_RETRY_SECONDS",
        0.0,
    ):
        try:
            read_json(denied_path)
        except PermissionError:
            pass
        else:
            raise AssertionError("persistent Windows permission denial must fail closed")

    posix_denied_path = SharingConflictPath('{"status": "hidden"}\n', failures=1)
    with patch.object(state_module, "os", SimpleNamespace(name="posix")), patch.object(
        state_module.time,
        "sleep",
    ) as posix_sleep:
        try:
            read_json(posix_denied_path)
        except PermissionError:
            pass
        else:
            raise AssertionError("non-Windows permission denial must fail immediately")
        assert_true(posix_denied_path.reads == 1, "non-Windows permission denial retried")
        posix_sleep.assert_not_called()

    with tempfile.TemporaryDirectory(prefix="costmarshal-cmd-shim-") as directory:
        shim_root = Path(directory)
        shim = shim_root / "codex.cmd"
        javascript = (
            shim_root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        )
        node = shim_root / "node.exe"
        javascript.parent.mkdir(parents=True)
        shim.write_text("@echo off\n", encoding="utf-8")
        javascript.write_text("// fixture\n", encoding="utf-8")
        node.write_bytes(b"fixture")
        # Patch the actor runner's OS dependency, not ``os.name`` on the shared
        # stdlib module.  Mutating the latter makes ``pathlib.Path`` try to
        # instantiate ``WindowsPath`` on Linux before the shim contract can be
        # exercised.
        with patch(
            "costmarshal_v2.actor_runner.os",
            SimpleNamespace(name="nt"),
        ):
            resolved = _resolve_windows_codex_shim(
                [str(shim), "--model", "gpt-safe", "-"]
            )
            assert_true(
                resolved == [str(node.resolve()), str(javascript.resolve()), "--model", "gpt-safe", "-"],
                "Windows codex.cmd must be replaced with a native Node argv",
            )
            try:
                process_argv([str(shim), "--model", "x&echo INJECTED", "-"])
            except SystemExit as exc:
                assert_true(
                    "batch actor commands are rejected" in str(exc),
                    "Windows batch rejection should explain the safety boundary",
                )
            else:
                raise AssertionError("Windows batch command reached process launch")

    with (
        patch("costmarshal_v2.actor_runner.sys.platform", "linux"),
        patch("costmarshal_v2.actor_runner.pid_start_marker", return_value="linux-proc:boot:1"),
    ):
        try:
            _runner_process_start_marker("local")
        except SystemExit as exc:
            assert_true("linux-proc-v2" in str(exc), "Linux local marker failure should be explicit")
        else:
            raise AssertionError("Linux local registration accepted a marker without group/token authority")
    v2_marker = "linux-proc-v2:boot:1:10:10:" + "a" * 64
    with (
        patch("costmarshal_v2.actor_runner.sys.platform", "linux"),
        patch("costmarshal_v2.actor_runner.pid_start_marker", return_value=v2_marker),
    ):
        assert_true(
            _runner_process_start_marker("local") == v2_marker,
            "Linux local registration should preserve the v2 group/token marker",
        )
    with (
        patch("costmarshal_v2.actor_runner.sys.platform", "linux"),
        patch("costmarshal_v2.actor_runner.pid_start_marker", return_value="linux-proc:boot:1"),
    ):
        assert_true(
            _runner_process_start_marker("tmux") == "linux-proc:boot:1",
            "tmux authority must not be confused with the local supervisor chain",
        )

    assert_true(slugify(" Agent V2/0001 ") == "agent-v2-0001", "slugify should normalize actor names")
    assert_true(actor_runtime_name("agent:V2 0001") == "agent-v2-0001", "actor runtime names should be stable slugs")
    assert_true(actor_target("cmv2-demo", "agent:V2 0001") == "cmv2-demo:agent-v2-0001", "runtime targets should combine session/actor")
    assert_true(select_backend_kind("local") == "local", "backend selection should allow explicit local")
    assert_true(select_backend_kind("tmux") == "tmux", "backend selection should allow explicit tmux")
    truncated_session = default_backend_session_name(
        "20260716-212345-leader-change-preview-and-apply"
    )
    assert_true(
        validate_tmux_name(truncated_session, label="default session") == truncated_session,
        "default session truncation must not leave a trailing separator",
    )
    required_direct_actor = {
        "role": "agent",
        "isolation": {"mode": "required"},
        "runtime": {},
    }
    with patch(
        "costmarshal_v2.actor_runner.control_store_enabled",
        return_value=False,
    ), patch(
        "costmarshal_v2.actor_runner.load_stable_governance_project",
        return_value={"governance": {"mode": "off", "ready": False}},
    ), patch(
        "costmarshal_v2.actor_runner.enforce_governance_contract",
        return_value={"governed": False},
    ):
        try:
            _validate_actor_governance_before_side_effect(
                ProjectLayout(root=Path("."), project_dir=Path(".")),
                required_direct_actor,
            )
        except SystemExit as exc:
            assert "Required OCI start needs the recoverable control store" in str(exc)
        else:
            raise AssertionError("direct required actor start bypassed the control-store gate")
        recovery_actor = json.loads(json.dumps(required_direct_actor))
        recovery_actor["runtime"]["container_id"] = "a" * 64
        assert_true(
            _validate_actor_governance_before_side_effect(
                ProjectLayout(root=Path("."), project_dir=Path(".")),
                recovery_actor,
            )
            is True,
            "pre-cutover required actor recovery should be attach/cleanup only",
        )
    assert_true(tmux_session_target("cmv2-demo") == "=cmv2-demo", "tmux sessions should use exact targets")
    assert_true(tmux_new_window_target("cmv2-demo") == "=cmv2-demo:", "tmux new windows should use exact session targets")
    assert_true(
        tmux_actor_target("cmv2-demo:agent-v2-0001") == "=cmv2-demo:=agent-v2-0001",
        "tmux actors should use exact session and window targets",
    )
    assert_true(
        tmux_format_literal("/tmp/a#{session_name}/b") == "/tmp/a##{session_name}/b",
        "tmux format literals should escape hash expansion",
    )
    for invalid_name in ("bad.name", "bad:name", "bad#name", "bad*name", "-bad", "bad-", "a" * 65):
        try:
            validate_tmux_name(invalid_name, label="test name")
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"tmux name validation accepted {invalid_name!r}")
    for invalid_target in ("cmv2-demo", "cmv2-demo:bad.name", "cmv2-demo:agent:extra", "=cmv2-demo:=agent"):
        try:
            validate_tmux_target(invalid_target)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"tmux target validation accepted {invalid_target!r}")
    assert_true(
        validate_persisted_tmux_actor_name("legacy.actor") == "legacy.actor"
        and validate_persisted_tmux_session_name("cmv2.legacy") == "cmv2.legacy"
        and validate_persisted_tmux_target("cmv2-demo:legacy.actor")
        == "cmv2-demo:legacy.actor",
        "persisted dotted actor names should remain available for safe shutdown",
    )
    runtime_session = {"backend": {"kind": "tmux", "session_name": "cmv2-demo"}}
    runtime_actor = {
        "id": "agent-v2-0001",
        "runtime": {
            "backend": "tmux",
            "session_name": "cmv2-demo",
            "actor_name": "agent-v2-0001",
            "target": "cmv2-demo:agent-v2-0001",
        },
    }
    validate_actor_runtime_binding(runtime_session, runtime_actor)
    legacy_runtime_actor = {
        "id": "legacy.actor",
        "runtime": {
            "backend": "tmux",
            "session_name": "cmv2-demo",
            "actor_name": "legacy.actor",
            "target": "cmv2-demo:legacy.actor",
        },
    }
    validate_actor_runtime_binding(runtime_session, legacy_runtime_actor)
    legacy_session = {"backend": {"kind": "tmux", "session_name": "cmv2.legacy"}}
    legacy_runtime_actor["runtime"]["session_name"] = "cmv2.legacy"
    legacy_runtime_actor["runtime"]["target"] = "cmv2.legacy:legacy.actor"
    validate_actor_runtime_binding(legacy_session, legacy_runtime_actor)
    effect_actor = {
        **runtime_actor,
        "task_id": "V2-0001",
        "attempt_id": "ATT-1",
        "launch_token": "launch-token",
        "profile_binding": {"sha256": "sha256:" + "a" * 64},
    }
    spawn_payload = _spawn_effect_payload(effect_actor)
    assert_true(
        spawn_payload["runtime_backend"] == "tmux"
        and spawn_payload["runtime_session_name"] == "cmv2-demo"
        and spawn_payload["runtime_actor_name"] == "agent-v2-0001"
        and spawn_payload["runtime_target"] == "cmv2-demo:agent-v2-0001",
        "spawn effects should freeze the admitted tmux runtime identity",
    )
    valid_observation = {
        "actor_id": "agent-v2-0001",
        "task_id": "V2-0001",
        "attempt_id": "ATT-1",
        "backend": "tmux",
        "runtime_target": "cmv2-demo:agent-v2-0001",
        "launch_token_sha256": spawn_payload["launch_token_sha256"],
        "recovery_generation": spawn_payload["recovery_generation"],
    }
    spawn_effect = {
        "payload": spawn_payload,
        "generation": spawn_payload["recovery_generation"],
    }
    _validate_spawn_observation(spawn_effect, valid_observation)
    corrupt_observation = {**valid_observation, "runtime_target": "victim:leader"}
    try:
        _validate_spawn_observation(spawn_effect, corrupt_observation)
    except ValueError:
        pass
    else:
        raise AssertionError("spawn observation accepted a tmux target outside its effect fence")
    corrupt_token_observation = {**valid_observation, "launch_token_sha256": "0" * 64}
    try:
        _validate_spawn_observation(spawn_effect, corrupt_token_observation)
    except ValueError:
        pass
    else:
        raise AssertionError("spawn observation accepted a launch token outside its effect fence")
    corrupt_generation_observation = {**valid_observation, "recovery_generation": 1}
    try:
        _validate_spawn_observation(spawn_effect, corrupt_generation_observation)
    except ValueError:
        pass
    else:
        raise AssertionError("spawn observation accepted a stale recovery generation")
    effect_actor["runtime"].update({"container_name": "worker-1", "container_id": "b" * 64})
    stop_payload = _stop_effect_payload(effect_actor, reason="unit")
    assert_true(
        stop_payload["container_name"] == "worker-1" and stop_payload["container_id"] == "b" * 64,
        "stop effects should freeze available OCI container identity",
    )
    effect_actor["runtime"].pop("container_name")
    effect_actor["runtime"].pop("container_id")
    for field, corrupt_value in (
        ("session_name", "victim"),
        ("actor_name", "leader"),
        ("target", "victim:leader"),
    ):
        original = runtime_actor["runtime"][field]
        runtime_actor["runtime"][field] = corrupt_value
        try:
            validate_actor_runtime_binding(runtime_session, runtime_actor)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"runtime binding accepted corrupt {field}")
        finally:
            runtime_actor["runtime"][field] = original
    numeric_actor = {
        "id": 123,
        "runtime": {
            "backend": "tmux",
            "session_name": "cmv2-demo",
            "actor_name": "123",
            "target": "cmv2-demo:123",
        },
    }
    try:
        validate_actor_runtime_binding(runtime_session, numeric_actor)
    except RuntimeError:
        pass
    else:
        raise AssertionError("runtime binding accepted a non-string actor id")

    assert_true(can_transition_task("planned", "dispatched"), "planned should dispatch")
    assert_true(can_transition_task("dispatched", "running"), "dispatched should become running")
    assert_true(can_transition_task("running", "done"), "running should finish")
    assert_true(not can_transition_task("done", "waiting_leader"), "terminal tasks should not reopen implicitly")
    assert_true(normalize_claim_path(r"Reports\\Shared.md") == "reports/shared.md", "claim paths should normalize separators and case")
    assert_true(paths_conflict("reports", "reports/shared.md"), "parent directory claims should conflict with children")
    assert_true(paths_conflict("reports/shared.md", "reports/shared.md"), "identical claims should conflict")
    assert_true(not paths_conflict("reports/a.md", "reports/b.md"), "sibling files should not conflict")
    results = summarize_results(
        [
            {
                "status": "done",
                "agent": "deepseek",
                "accepted_by_leader": True,
                "quality_score": 4,
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "estimated_cost_cny": 0.01,
            },
            {
                "status": "escalate",
                "agent": "kimi",
                "needs_escalation": True,
                "quality_score": 2,
                "estimated_cost_cny": None,
            },
        ]
    )
    assert_true(results["count"] == 2 and results["accepted"] == 1, "result summary should count accepted attempts")
    assert_true(results["escalated"] == 1, "result summary should count escalations")
    assert_true(results["avg_quality"] == 3.0, "result summary should average quality scores")
    assert_true(results["estimated_cost_cny"] == 0.01 and results["unknown_cost_count"] == 1, "result summary should separate known and unknown costs")
    leader_work = summarize_leader_self_work(
        [
            {
                "work_type": "verification",
                "risk": "low",
                "minutes": 2,
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "estimated_cost_cny": 0.02,
            }
        ]
    )
    assert_true(leader_work["count"] == 1 and leader_work["total_minutes"] == 2, "leader self-work summary should count minutes")
    assert_true(leader_work["by_type"]["verification"] == 1 and leader_work["by_risk"]["low"] == 1, "leader self-work summary should bucket rows")

    with tempfile.TemporaryDirectory(prefix="costmarshal-v2-unit-") as tmp:
        project = Path(tmp) / "project"
        layout = ProjectLayout(root=Path(tmp), project_dir=project)
        assert_true(layout.root == Path(tmp).resolve(), "project layout root should be canonical")
        assert_true(layout.project_dir == project.resolve(), "project directory should be canonical")
        layout.actors_dir.mkdir(parents=True)
        duplicate_runtime_actor = {
            "id": "other-actor",
            "role": "agent",
            "runtime": {
                "backend": "tmux",
                "session_name": "cmv2-demo",
                "actor_name": "agent-v2-0001",
                "target": "cmv2-demo:agent-v2-0001",
            },
        }
        (layout.actors_dir / "other-actor.json").write_text(
            json.dumps(duplicate_runtime_actor),
            encoding="utf-8",
        )
        try:
            validate_actor_runtime_authority(layout, runtime_session, runtime_actor)
        except RuntimeError:
            pass
        else:
            raise AssertionError("tmux runtime authority accepted a duplicate actor window name")
        stopped_actor = {
            "id": "stopped-actor",
            "role": "agent",
            "attempt_id": "ATT-STOP",
            "runtime": {
                "backend": "local",
                "process_start_marker": "marker-1",
                "container_name": "worker-1",
                "container_id": "c" * 64,
            },
        }
        stopped_actor_path = layout.actors_dir / "stopped-actor.json"
        stopped_actor_path.write_text(json.dumps(stopped_actor), encoding="utf-8")
        stopped_effect = {
            "effect_type": STOP_EFFECT_TYPE,
            "payload": _stop_effect_payload(stopped_actor, reason="unit"),
        }
        _validate_stop_effect(layout, stopped_effect)
        stopped_actor["runtime"]["container_name"] = "victim-container"
        stopped_actor_path.write_text(json.dumps(stopped_actor), encoding="utf-8")
        try:
            _validate_stop_effect(layout, stopped_effect)
        except ValueError:
            pass
        else:
            raise AssertionError("stop effect accepted a container identity outside its fence")
        monotonic_actor = {
            "id": "monotonic-actor",
            "role": "agent",
            "attempt_id": "ATT-MONOTONIC",
            "runtime": {"backend": "local"},
        }
        monotonic_path = layout.actors_dir / "monotonic-actor.json"
        monotonic_effect = {
            "effect_type": STOP_EFFECT_TYPE,
            "payload": _stop_effect_payload(monotonic_actor, reason="race"),
        }
        monotonic_actor["runtime"].update(
            {
                "process_start_marker": "registered-after-queue",
                "container_name": "registered-container",
                "container_id": "d" * 64,
            }
        )
        monotonic_path.write_text(json.dumps(monotonic_actor), encoding="utf-8")
        _validate_stop_effect(layout, monotonic_effect)
        actor = {
            "id": "agent-v2-0001",
            "task_id": "V2-0001",
            "model": "gpt-5",
            "mailbox": {"dir": "scheduler/mailboxes/agent-v2-0001"},
            "prompt_path": "scheduler/actors/agent-v2-0001.prompt.md",
        }
        session = {"project_id": "P-1"}
        formatted = format_actor_command(
            "codex --model {model} --project {project} --task {task} --mailbox {mailbox} --prompt {prompt_file} --brief {brief} --report {report}",
            layout=layout,
            session=session,
            actor=actor,
        )
        assert_true("gpt-5" in formatted and "V2-0001" in formatted, "actor command should substitute known fields")
        assert_true("agent-v2-0001.prompt.md" in formatted, "actor command should substitute prompt_file")
        assert_true("brief.md" in formatted and "completion-report.md" in formatted, "actor command should substitute task files")
        assert_true(format_actor_command("codex {unknown}", layout=layout, session=session, actor=actor) == "codex {unknown}", "unknown template fields should remain safe")
        assert_true(relpath(project / "tasks" / "V2-0001", project) == "tasks/V2-0001", "relpath should use project-relative paths")

    rendered = command_to_string(["tmux", "new-session", "-s", "demo"])
    assert_true("tmux" in rendered and "demo" in rendered, "command_to_string should render commands for diagnostics")
    print("v2 unit ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
