#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from costmarshal_v2.context_projection import (  # noqa: E402
    ChangeOperation,
    build_cumulative_change_manifest,
    persist_change_artifact,
)
from costmarshal_v2.paths import ProjectLayout  # noqa: E402
from costmarshal_v2.control_store import control_transaction, effect_status  # noqa: E402
from costmarshal_v2 import scheduler as scheduler_module  # noqa: E402
from costmarshal_v2.scheduler import (  # noqa: E402
    command_apply_changes,
    command_preview_changes,
    command_record_result,
    process_runtime_effects,
)
from costmarshal_v2.state import load_project, load_task, save_task  # noqa: E402


CLI = ROOT / "scripts" / "costmarshal.py"


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class ChangeWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="costmarshal-change-workflow-")
        self.temp = Path(self.temporary.name)
        self.workspace = self.temp / "workspace"
        self.workspace.mkdir()
        git(self.workspace, "init", "--quiet")
        git(self.workspace, "config", "user.name", "CostMarshal Test")
        git(self.workspace, "config", "user.email", "costmarshal@example.invalid")
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "app.py").write_text("base\n", encoding="utf-8")
        git(self.workspace, "add", "src/app.py")
        git(self.workspace, "commit", "--quiet", "-m", "base")
        self.base_sha = git(self.workspace, "rev-parse", "HEAD")
        initialized = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--root",
                str(self.temp / "runtime"),
                "init",
                "--objective",
                "leader change preview and apply",
                "--workspace",
                str(self.workspace),
                "--governance",
                "off",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if initialized.returncode:
            raise AssertionError(initialized.stderr)
        project_dir = Path(json.loads(initialized.stdout)["project"])
        self.layout = ProjectLayout(root=self.temp / "runtime", project_dir=project_dir)
        self.project = load_project(self.layout)
        self.attempt_id = "ATT-change-workflow-001"
        task_dir = self.layout.tasks_dir / "V2-0001"
        task_dir.mkdir(parents=True)
        save_task(
            self.layout,
            {
                "id": "V2-0001",
                "status": "waiting_leader",
                "attempts": [
                    {
                        "attempt_id": self.attempt_id,
                        "status": "waiting_leader",
                        "isolation": {"mode": "required"},
                    }
                ],
            },
        )
        prepared = build_cumulative_change_manifest(
            base_sha=self.base_sha,
            write_scope=["src/app.py"],
            operations=[ChangeOperation.upsert("src/app.py", b"reviewed\n")],
        )
        persisted = persist_change_artifact(
            self.layout.root
            / "task-change-artifacts"
            / self.project["project_id"]
            / "v2-0001",
            prepared,
        )
        self.manifest = prepared.manifest
        self.artifact_root = persisted.artifact_root
        self.contract = {
            "base_sha": self.base_sha,
            "contract_sha256": "sha256:" + "1" * 64,
        }
        self.output = {
            "attempt_output_sha256": "sha256:" + "2" * 64,
            "outgoing_changes": {
                "manifest_sha256": prepared.manifest_sha256,
                "change_count": 1,
                "total_upsert_bytes": len(b"reviewed\n"),
            },
        }
        self.preview_root = (
            self.layout.root
            / "change-previews"
            / self.project["project_id"]
            / "v2-0001"
            / self.attempt_id.lower()
        ).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def semantic_inputs(self):
        return (
            self.contract,
            self.output,
            ("src/app.py",),
            self.manifest,
            self.artifact_root,
            self.preview_root,
        )

    def args(self, **overrides):
        values = {
            "root": self.layout.root,
            "project": str(self.layout.project_dir),
            "task": "V2-0001",
            "attempt": self.attempt_id,
            "command_id": "CMD-change-preview-001",
            "apply": False,
            "preview_sha": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_preview_then_explicit_apply_is_durable_and_idempotent(self) -> None:
        with mock.patch(
            "costmarshal_v2.scheduler._semantic_change_inputs",
            side_effect=lambda *_args: self.semantic_inputs(),
        ):
            command_preview_changes(self.args())
            replay_task = load_task(self.layout, "V2-0001")
            receipt = replay_task["attempts"][0]["change_preview_receipt"]
            self.assertTrue(Path(receipt["patch_path"]).is_file())
            self.assertEqual(git(self.workspace, "status", "--porcelain"), "")
            command_preview_changes(self.args())
            with mock.patch(
                "costmarshal_v2.scheduler.prepare_change_preview",
                side_effect=AssertionError("conflicting command must fail before Git"),
            ), self.assertRaisesRegex(SystemExit, "different command-id"):
                command_preview_changes(
                    self.args(command_id="CMD-change-preview-conflicting")
                )
            self.assertEqual(
                load_task(self.layout, "V2-0001")["attempts"][0][
                    "change_preview_receipt"
                ],
                receipt,
            )

            attempt = replay_task["attempts"][0]
            attempt.update(
                {
                    "recorded_result_status": "done",
                    "accepted_by_leader": True,
                    "leader_result_id": "RES-change-workflow-001",
                    "attempt_input": {"fixture": True},
                }
            )
            save_task(self.layout, replay_task)
            trusted = {
                "id": "RES-change-workflow-001",
                "attempt_id": self.attempt_id,
            }
            apply_contract = {
                "preview_sha256": "sha256:" + "3" * 64,
            }
            with mock.patch(
                "costmarshal_v2.scheduler.trusted_result_rows",
                return_value=[trusted],
            ), mock.patch(
                "costmarshal_v2.scheduler.build_apply_preview_contract",
                return_value=apply_contract,
            ):
                command_apply_changes(self.args(command_id=None))
                command_apply_changes(
                    self.args(
                        command_id="CMD-change-apply-001",
                        apply=True,
                        preview_sha=apply_contract["preview_sha256"],
                    )
                )
                applied_task = load_task(self.layout, "V2-0001")
                applied = applied_task["attempts"][0]["change_apply_receipt"]
                self.assertEqual(applied["outcome"]["status"], "applied")
                self.assertEqual(
                    git(self.workspace, "show", ":src/app.py"),
                    "reviewed",
                )
                git(
                    self.workspace,
                    "restore",
                    "--staged",
                    "--worktree",
                    "--",
                    "src/app.py",
                )
                self.assertEqual(git(self.workspace, "show", ":src/app.py"), "base")
                command_apply_changes(
                    self.args(
                        command_id="CMD-change-apply-001",
                        apply=True,
                        preview_sha=apply_contract["preview_sha256"],
                    )
                )
                self.assertEqual(git(self.workspace, "show", ":src/app.py"), "reviewed")

    def test_sqlite_cutover_recovers_preview_and_apply_through_effect_outbox(self) -> None:
        migrated = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--root",
                str(self.layout.root),
                "migrate-state",
                "--project",
                str(self.layout.project_dir),
                "--apply",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if migrated.returncode:
            raise AssertionError(migrated.stderr)
        with self.assertRaisesRegex(SystemExit, "leading or trailing whitespace"):
            command_preview_changes(
                self.args(command_id=" CMD-change-preview-whitespace ")
            )
        connection = sqlite3.connect(self.layout.scheduler_dir / "state.db")
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM effects").fetchone()[0],
                0,
            )
        finally:
            connection.close()
        trusted = {
            "id": "RES-change-workflow-sqlite-001",
            "attempt_id": self.attempt_id,
        }
        apply_contract = {"preview_sha256": "sha256:" + "4" * 64}
        with mock.patch(
            "costmarshal_v2.scheduler._frozen_change_state_inputs",
            side_effect=lambda *_args: self.semantic_inputs(),
        ), mock.patch(
            "costmarshal_v2.scheduler._semantic_change_inputs",
            side_effect=lambda *_args: self.semantic_inputs(),
        ), mock.patch(
            "costmarshal_v2.scheduler.trusted_result_rows",
            return_value=[trusted],
        ), mock.patch(
            "costmarshal_v2.scheduler.build_apply_preview_contract",
            return_value=apply_contract,
        ):
            command_preview_changes(
                self.args(command_id="CMD-change-preview-sqlite-outbox")
            )
            preview_effect_id = scheduler_module._git_effect_id(
                scheduler_module.GIT_PREVIEW_EFFECT_TYPE,
                "CMD-change-preview-sqlite-outbox",
            )
            self.assertEqual(
                effect_status(self.layout, preview_effect_id)["status"],
                "applied",
            )
            previewed = load_task(self.layout, "V2-0001")
            self.assertIn(
                "change_preview_receipt",
                previewed["attempts"][0],
            )
            self.assertEqual(git(self.workspace, "status", "--porcelain"), "")
            with mock.patch(
                "costmarshal_v2.scheduler.prepare_change_preview",
                side_effect=AssertionError("conflicting command must fail before Git"),
            ), self.assertRaisesRegex(SystemExit, "different command-id"):
                command_preview_changes(
                    self.args(command_id="CMD-change-preview-sqlite-conflicting")
                )

            with control_transaction(
                self.layout,
                command_name="fixture_accept_change",
                command_id="CMD-fixture-accept-change",
                payload={"task_id": "V2-0001", "attempt_id": self.attempt_id},
            ) as transaction:
                if not transaction.replay:
                    accepted = load_task(self.layout, "V2-0001")
                    accepted["attempts"][0].update(
                        {
                            "recorded_result_status": "done",
                            "accepted_by_leader": True,
                            "leader_result_id": trusted["id"],
                            "attempt_input": {"fixture": True},
                        }
                    )
                    save_task(self.layout, accepted)

            command_apply_changes(
                self.args(
                    command_id="CMD-change-apply-sqlite-outbox",
                    apply=True,
                    preview_sha=apply_contract["preview_sha256"],
                )
            )
            apply_effect_id = scheduler_module._git_effect_id(
                scheduler_module.GIT_APPLY_EFFECT_TYPE,
                "CMD-change-apply-sqlite-outbox",
            )
            self.assertEqual(
                effect_status(self.layout, apply_effect_id)["status"],
                "applied",
            )
            applied = load_task(self.layout, "V2-0001")["attempts"][0][
                "change_apply_receipt"
            ]
            self.assertEqual(
                applied["outcome"]["status"],
                "reviewed_patch_staged",
            )
            self.assertEqual(git(self.workspace, "show", ":src/app.py"), "reviewed")

    def test_sqlite_apply_crashes_before_observe_and_after_projection_converge(self) -> None:
        migrated = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--root",
                str(self.layout.root),
                "migrate-state",
                "--project",
                str(self.layout.project_dir),
                "--apply",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if migrated.returncode:
            raise AssertionError(migrated.stderr)
        trusted = {
            "id": "RES-change-workflow-crash-001",
            "attempt_id": self.attempt_id,
        }
        apply_contract = {"preview_sha256": "sha256:" + "7" * 64}

        def expire(effect_id: str) -> None:
            connection = sqlite3.connect(self.layout.scheduler_dir / "state.db")
            try:
                connection.execute(
                    "UPDATE effects SET lease_expires_at=? WHERE effect_id=?",
                    ("2000-01-01T00:00:00.000000+00:00", effect_id),
                )
                connection.commit()
            finally:
                connection.close()

        def crash_at(expected: str):
            def injected(name: str) -> None:
                if name == expected:
                    raise KeyboardInterrupt(expected)

            return injected

        with mock.patch(
            "costmarshal_v2.scheduler._frozen_change_state_inputs",
            side_effect=lambda *_args: self.semantic_inputs(),
        ), mock.patch(
            "costmarshal_v2.scheduler._semantic_change_inputs",
            side_effect=lambda *_args: self.semantic_inputs(),
        ), mock.patch(
            "costmarshal_v2.scheduler.trusted_result_rows",
            return_value=[trusted],
        ), mock.patch(
            "costmarshal_v2.scheduler.build_apply_preview_contract",
            return_value=apply_contract,
        ):
            preview_args = self.args(command_id="CMD-change-preview-crash-outbox")
            with control_transaction(
                self.layout,
                command_name="command_preview_changes",
                command_id=preview_args.command_id,
                payload={"fixture": "preview-crash-boundary"},
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    command_preview_changes(preview_args)
            preview_effect_id = scheduler_module._git_effect_id(
                scheduler_module.GIT_PREVIEW_EFFECT_TYPE,
                preview_args.command_id,
            )
            with mock.patch(
                "costmarshal_v2.scheduler._scheduler_fault",
                side_effect=crash_at("git_preview.after_external_before_observe"),
            ), self.assertRaises(KeyboardInterrupt):
                process_runtime_effects(
                    self.layout,
                    limit=1,
                    _effect_ids=(preview_effect_id,),
                    _governance_prevalidated=True,
                )
            preview_first = effect_status(self.layout, preview_effect_id)
            self.assertEqual(preview_first["status"], "leased")
            self.assertIsNone(preview_first["observation"])
            published_patches = list(self.preview_root.glob("patches/sha256/*"))
            self.assertEqual(len(published_patches), 1)
            published_bytes = published_patches[0].read_bytes()
            expire(preview_effect_id)
            process_runtime_effects(
                self.layout,
                limit=1,
                _effect_ids=(preview_effect_id,),
                _governance_prevalidated=True,
            )
            preview_final = effect_status(self.layout, preview_effect_id)
            self.assertEqual(preview_final["status"], "applied")
            self.assertEqual(preview_final["attempts"], 2)
            self.assertEqual(published_patches[0].read_bytes(), published_bytes)
            with control_transaction(
                self.layout,
                command_name="fixture_accept_change_crash",
                command_id="CMD-fixture-accept-change-crash",
                payload={"task_id": "V2-0001", "attempt_id": self.attempt_id},
            ) as transaction:
                if not transaction.replay:
                    accepted = load_task(self.layout, "V2-0001")
                    accepted["attempts"][0].update(
                        {
                            "recorded_result_status": "done",
                            "accepted_by_leader": True,
                            "leader_result_id": trusted["id"],
                            "attempt_input": {"fixture": True},
                        }
                    )
                    save_task(self.layout, accepted)

            apply_args = self.args(
                command_id="CMD-change-apply-crash-outbox",
                apply=True,
                preview_sha=apply_contract["preview_sha256"],
            )
            with control_transaction(
                self.layout,
                command_name="command_apply_changes",
                command_id=apply_args.command_id,
                payload={"fixture": "crash-boundaries"},
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    command_apply_changes(apply_args)
            effect_id = scheduler_module._git_effect_id(
                scheduler_module.GIT_APPLY_EFFECT_TYPE,
                apply_args.command_id,
            )

            with mock.patch(
                "costmarshal_v2.scheduler._scheduler_fault",
                side_effect=crash_at("git_apply.after_external_before_observe"),
            ), self.assertRaises(KeyboardInterrupt):
                process_runtime_effects(
                    self.layout,
                    limit=1,
                    _effect_ids=(effect_id,),
                    _governance_prevalidated=True,
                )
            first = effect_status(self.layout, effect_id)
            self.assertEqual(first["status"], "leased")
            self.assertIsNone(first["observation"])
            self.assertEqual(git(self.workspace, "show", ":src/app.py"), "reviewed")
            self.assertNotIn(
                "change_apply_receipt",
                load_task(self.layout, "V2-0001")["attempts"][0],
            )

            expire(effect_id)
            with mock.patch(
                "costmarshal_v2.scheduler._scheduler_fault",
                side_effect=crash_at("git_apply.after_observe_before_projection"),
            ), self.assertRaises(KeyboardInterrupt):
                process_runtime_effects(
                    self.layout,
                    limit=1,
                    _effect_ids=(effect_id,),
                    _governance_prevalidated=True,
                )
            second = effect_status(self.layout, effect_id)
            self.assertEqual(second["status"], "observed")
            self.assertEqual(
                second["observation"]["status"],
                "reviewed_patch_staged",
            )
            self.assertNotIn(
                "change_apply_receipt",
                load_task(self.layout, "V2-0001")["attempts"][0],
            )

            expire(effect_id)
            with mock.patch(
                "costmarshal_v2.scheduler._scheduler_fault",
                side_effect=crash_at("git_apply.after_projection_before_apply"),
            ), self.assertRaises(KeyboardInterrupt):
                process_runtime_effects(
                    self.layout,
                    limit=1,
                    _effect_ids=(effect_id,),
                    _governance_prevalidated=True,
                )
            third = effect_status(self.layout, effect_id)
            self.assertEqual(third["status"], "observed")
            projected_receipt = load_task(self.layout, "V2-0001")["attempts"][0][
                "change_apply_receipt"
            ]

            expire(effect_id)
            process_runtime_effects(
                self.layout,
                limit=1,
                _effect_ids=(effect_id,),
                _governance_prevalidated=True,
            )
            final = effect_status(self.layout, effect_id)
            self.assertEqual(final["status"], "applied")
            self.assertEqual(final["attempts"], 4)
            self.assertEqual(
                load_task(self.layout, "V2-0001")["attempts"][0][
                    "change_apply_receipt"
                ],
                projected_receipt,
            )

    def test_record_result_rechecks_preview_source_freshness_before_acceptance(self) -> None:
        with mock.patch(
            "costmarshal_v2.scheduler._semantic_change_inputs",
            side_effect=lambda *_args: self.semantic_inputs(),
        ):
            command_preview_changes(self.args())

        task = load_task(self.layout, "V2-0001")
        attempt = task["attempts"][0]
        attempt.update(
            {
                "provider": "fixture-provider",
                "tier": "low",
                "model": "fixture-model",
                "profile": "fixture-profile",
                "execution_identity": {
                    "model": "fixture-model",
                    "profile": "fixture-profile",
                    "profile_sha256": "sha256:" + "5" * 64,
                },
                "profile_binding": {"sha256": "sha256:" + "5" * 64},
                "attempt_output": self.output,
                "attempt_output_sha256": self.output["attempt_output_sha256"],
            }
        )
        save_task(self.layout, task)
        (self.workspace / "concurrent.txt").write_text(
            "not part of the reviewed source\n", encoding="utf-8"
        )
        sealed_output = {
            **self.output,
            "report_receipt": {"sha256": "sha256:" + "6" * 64},
        }
        result_args = SimpleNamespace(
            root=self.layout.root,
            project=str(self.layout.project_dir),
            task="V2-0001",
            attempt=self.attempt_id,
            command_id="CMD-stale-preview-accept",
            status="done",
            accepted_by_leader=True,
            quality_score=5,
            model=None,
            handoff=None,
        )
        with mock.patch(
            "costmarshal_v2.scheduler._validate_required_attempt_output",
            return_value=sealed_output,
        ), mock.patch(
            "costmarshal_v2.scheduler._semantic_change_inputs",
            side_effect=lambda *_args: self.semantic_inputs(),
        ):
            with self.assertRaisesRegex(SystemExit, "clean"):
                command_record_result(result_args)
        rejected = load_task(self.layout, "V2-0001")["attempts"][0]
        self.assertNotIn("leader_result_id", rejected)
        self.assertEqual(self.layout.results_jsonl.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    program = unittest.main(verbosity=2, exit=False)
    if program.result.wasSuccessful() and len(sys.argv) == 1:
        print(
            "COSTMARSHAL_RUNTIME_EVIDENCE="
            + json.dumps(
                {
                    "schema_version": 1,
                    "test": "tests/change_workflow_test.py",
                    "crash_points": [
                        "git_preview.after_external_before_observe",
                        "git_apply.after_external_before_observe",
                        "git_apply.after_observe_before_projection",
                        "git_apply.after_projection_before_apply",
                    ],
                    "recovery_scenarios": [
                        "git_preview_content_addressed_publication_replay",
                        "git_apply_external_replay_canonicalized",
                        "git_apply_observed_projection_replay",
                        "git_apply_projected_command_completion_replay",
                    ],
                    "provider_calls": 0,
                    "expected_provider_calls": 0,
                    "orphan_effects": 0,
                },
                sort_keys=True,
            )
        )
    raise SystemExit(0 if program.result.wasSuccessful() else 1)
