#!/usr/bin/env python3
from __future__ import annotations

import json
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
from costmarshal_v2.scheduler import (  # noqa: E402
    command_apply_changes,
    command_preview_changes,
    command_record_result,
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

    def test_sqlite_cutover_blocks_unrecoverable_git_apply_effect(self) -> None:
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
        with self.assertRaisesRegex(SystemExit, "recoverable effect outbox"):
            command_preview_changes(
                self.args(command_id="CMD-change-preview-sqlite-blocked")
            )
        with self.assertRaisesRegex(SystemExit, "recoverable effect outbox"):
            command_apply_changes(
                self.args(
                    command_id="CMD-change-apply-sqlite-blocked",
                    apply=True,
                    preview_sha="sha256:" + "4" * 64,
                )
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
    unittest.main(verbosity=2)
