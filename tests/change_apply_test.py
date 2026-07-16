#!/usr/bin/env python3
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
import os
from dataclasses import replace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from costmarshal_v2.change_apply import (  # noqa: E402
    ChangeApplyError,
    apply_prepared_change_preview,
    prepare_change_preview,
    prepared_change_preview_from_dict,
    require_prepared_change_source_ready,
)
from costmarshal_v2.context_projection import (  # noqa: E402
    ChangeOperation,
    build_cumulative_change_manifest,
    persist_change_artifact,
)


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


def git_bytes(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout


class ChangeApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = Path(tempfile.mkdtemp(prefix="costmarshal-change-apply-"))
        self.repository = self.temporary / "repository"
        self.repository.mkdir()
        git(self.repository, "init", "--quiet")
        git(self.repository, "config", "user.name", "CostMarshal Test")
        git(self.repository, "config", "user.email", "costmarshal@example.invalid")
        (self.repository / "src").mkdir()
        (self.repository / "src" / "app.py").write_text("base\n", encoding="utf-8")
        (self.repository / "src" / "old.txt").write_text("old\n", encoding="utf-8")
        git(self.repository, "add", "src")
        git(self.repository, "commit", "--quiet", "-m", "base")
        self.base_sha = git(self.repository, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        shutil.rmtree(self.temporary, ignore_errors=True)

    def change_artifact(self):
        prepared = build_cumulative_change_manifest(
            base_sha=self.base_sha,
            write_scope=["src"],
            operations=[
                ChangeOperation.upsert("src/app.py", b"changed\n"),
                ChangeOperation.delete("src/old.txt"),
                ChangeOperation.upsert("src/new.sh", b"#!/bin/sh\n", executable=True),
            ],
        )
        persisted = persist_change_artifact(self.temporary / "cas", prepared)
        return prepared, persisted

    def apply_preview(self, preview, prepared, persisted):
        return apply_prepared_change_preview(
            preview,
            expected_repository=self.repository,
            expected_base_sha=self.base_sha,
            expected_write_scope=["src"],
            expected_change_manifest_sha256=prepared.manifest_sha256,
            change_manifest=prepared.manifest,
            change_artifact_root=persisted.artifact_root,
            scratch_root=self.temporary / "apply-verification",
        )

    def test_preview_isolated_and_apply_is_head_cas_idempotent(self) -> None:
        prepared, persisted = self.change_artifact()
        preview = prepare_change_preview(
            self.repository,
            base_sha=self.base_sha,
            write_scope=["src"],
            change_manifest=prepared.manifest,
            change_artifact_root=persisted.artifact_root,
            preview_root=self.temporary / "previews",
        )
        self.assertEqual(
            preview.changed_paths,
            ("src/app.py", "src/new.sh", "src/old.txt"),
        )
        self.assertTrue(preview.patch_path.is_file())
        self.assertEqual(git(self.repository, "status", "--porcelain"), "")
        self.assertEqual((self.repository / "src" / "app.py").read_text(), "base\n")

        applied = self.apply_preview(preview, prepared, persisted)
        self.assertEqual(applied["status"], "applied")
        self.assertEqual((self.repository / "src" / "app.py").read_text(), "changed\n")
        self.assertFalse((self.repository / "src" / "old.txt").exists())
        self.assertEqual(git_bytes(self.repository, "show", ":src/new.sh"), b"#!/bin/sh\n")
        self.assertTrue(git(self.repository, "ls-files", "--stage", "src/new.sh").startswith("100755 "))
        self.assertEqual(git(self.repository, "write-tree"), preview.candidate_tree_sha)

        replay = self.apply_preview(preview, prepared, persisted)
        self.assertEqual(replay["status"], "already_applied")
        self.assertTrue(replay["staged"])

        (self.repository / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
        with self.assertRaisesRegex(ChangeApplyError, "dirty"):
            self.apply_preview(preview, prepared, persisted)

    def test_preview_and_apply_reject_dirty_or_changed_state(self) -> None:
        prepared, persisted = self.change_artifact()
        (self.repository / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(ChangeApplyError, "clean"):
            prepare_change_preview(
                self.repository,
                base_sha=self.base_sha,
                write_scope=["src"],
                change_manifest=prepared.manifest,
                change_artifact_root=persisted.artifact_root,
                preview_root=self.temporary / "dirty-preview",
            )
        (self.repository / "untracked.txt").unlink()
        preview = prepare_change_preview(
            self.repository,
            base_sha=self.base_sha,
            write_scope=["src"],
            change_manifest=prepared.manifest,
            change_artifact_root=persisted.artifact_root,
            preview_root=self.temporary / "clean-preview",
        )
        (self.repository / "src" / "app.py").write_text("concurrent\n", encoding="utf-8")
        with self.assertRaisesRegex(ChangeApplyError, "different|dirty"):
            self.apply_preview(preview, prepared, persisted)

    def test_empty_cumulative_change_is_a_verified_noop(self) -> None:
        prepared = build_cumulative_change_manifest(
            base_sha=self.base_sha,
            write_scope=["src"],
            operations=[],
        )
        persisted = persist_change_artifact(self.temporary / "empty-cas", prepared)
        preview = prepare_change_preview(
            self.repository,
            base_sha=self.base_sha,
            write_scope=["src"],
            change_manifest=prepared.manifest,
            change_artifact_root=persisted.artifact_root,
            preview_root=self.temporary / "empty-preview",
        )
        self.assertEqual(preview.changed_paths, ())
        self.assertEqual(preview.patch_size_bytes, 0)
        result = self.apply_preview(preview, prepared, persisted)
        self.assertEqual(result["status"], "nothing_to_apply")
        self.assertEqual(git(self.repository, "status", "--porcelain"), "")

    def test_apply_revalidates_patch_against_authoritative_manifest(self) -> None:
        authorized = build_cumulative_change_manifest(
            base_sha=self.base_sha,
            write_scope=["src/app.py"],
            operations=[ChangeOperation.upsert("src/app.py", b"authorized\n")],
        )
        authorized_cas = persist_change_artifact(
            self.temporary / "authorized-cas", authorized
        )
        different = build_cumulative_change_manifest(
            base_sha=self.base_sha,
            write_scope=["src/old.txt"],
            operations=[ChangeOperation.delete("src/old.txt")],
        )
        different_cas = persist_change_artifact(
            self.temporary / "different-cas", different
        )
        different_preview = prepare_change_preview(
            self.repository,
            base_sha=self.base_sha,
            write_scope=["src/old.txt"],
            change_manifest=different.manifest,
            change_artifact_root=different_cas.artifact_root,
            preview_root=self.temporary / "different-preview",
        )
        forged = replace(
            different_preview,
            write_scope=("src/app.py",),
            change_manifest_sha256=authorized.manifest_sha256,
            changed_paths=("src/app.py",),
        )
        with self.assertRaisesRegex(ChangeApplyError, "manifest|candidate index|reproduce"):
            apply_prepared_change_preview(
                forged,
                expected_repository=self.repository,
                expected_base_sha=self.base_sha,
                expected_write_scope=["src/app.py"],
                expected_change_manifest_sha256=authorized.manifest_sha256,
                change_manifest=authorized.manifest,
                change_artifact_root=authorized_cas.artifact_root,
                scratch_root=self.temporary / "forged-verification",
            )
        self.assertEqual(git(self.repository, "status", "--porcelain"), "")

    def test_git_environment_injection_is_removed(self) -> None:
        prepared, persisted = self.change_artifact()
        alternate_index = self.temporary / "attacker.index"
        previous = os.environ.get("GIT_INDEX_FILE")
        os.environ["GIT_INDEX_FILE"] = str(alternate_index)
        try:
            prepare_change_preview(
                self.repository,
                base_sha=self.base_sha,
                write_scope=["src"],
                change_manifest=prepared.manifest,
                change_artifact_root=persisted.artifact_root,
                preview_root=self.temporary / "environment-preview",
            )
        finally:
            if previous is None:
                os.environ.pop("GIT_INDEX_FILE", None)
            else:
                os.environ["GIT_INDEX_FILE"] = previous
        self.assertFalse(alternate_index.exists())
        self.assertEqual(git(self.repository, "status", "--porcelain"), "")

    def test_repository_checkout_hook_is_disabled_during_preview(self) -> None:
        prepared, persisted = self.change_artifact()
        marker = self.temporary / "hook-ran.txt"
        hooks = self.temporary / "hostile-hooks"
        hooks.mkdir()
        post_checkout = hooks / "post-checkout"
        post_checkout.write_text(
            "#!/bin/sh\nprintf ran > '" + marker.as_posix() + "'\n",
            encoding="utf-8",
        )
        post_checkout.chmod(0o755)
        git(self.repository, "config", "core.hooksPath", str(hooks))

        prepare_change_preview(
            self.repository,
            base_sha=self.base_sha,
            write_scope=["src"],
            change_manifest=prepared.manifest,
            change_artifact_root=persisted.artifact_root,
            preview_root=self.temporary / "hook-safe-preview",
        )
        self.assertFalse(marker.exists())

    def test_post_apply_cas_detects_concurrent_tracked_mutation(self) -> None:
        prepared = build_cumulative_change_manifest(
            base_sha=self.base_sha,
            write_scope=["src/app.py"],
            operations=[ChangeOperation.upsert("src/app.py", b"reviewed\n")],
        )
        persisted = persist_change_artifact(self.temporary / "race-cas", prepared)
        preview = prepare_change_preview(
            self.repository,
            base_sha=self.base_sha,
            write_scope=["src/app.py"],
            change_manifest=prepared.manifest,
            change_artifact_root=persisted.artifact_root,
            preview_root=self.temporary / "race-preview",
        )
        from costmarshal_v2 import change_apply as change_apply_module

        original_run_git = change_apply_module._run_git
        injected = False

        def racing_run_git(repository, arguments, **kwargs):
            nonlocal injected
            result = original_run_git(repository, arguments, **kwargs)
            if (
                not injected
                and Path(repository).resolve() == self.repository.resolve()
                and arguments == ["apply", "--index", "-"]
            ):
                injected = True
                (self.repository / "src" / "old.txt").write_text(
                    "concurrent\n", encoding="utf-8"
                )
            return result

        with mock.patch.object(change_apply_module, "_run_git", side_effect=racing_run_git):
            with self.assertRaisesRegex(ChangeApplyError, "uncertain"):
                apply_prepared_change_preview(
                    preview,
                    expected_repository=self.repository,
                    expected_base_sha=self.base_sha,
                    expected_write_scope=["src/app.py"],
                    expected_change_manifest_sha256=prepared.manifest_sha256,
                    change_manifest=prepared.manifest,
                    change_artifact_root=persisted.artifact_root,
                    scratch_root=self.temporary / "race-verification",
                )
        self.assertTrue(injected)

    def test_durable_preview_requires_intact_patch_and_fresh_clean_source(self) -> None:
        prepared, persisted = self.change_artifact()
        preview_root = self.temporary / "durable-preview"
        preview = prepare_change_preview(
            self.repository,
            base_sha=self.base_sha,
            write_scope=["src"],
            change_manifest=prepared.manifest,
            change_artifact_root=persisted.artifact_root,
            preview_root=preview_root,
        )
        require_prepared_change_source_ready(preview)

        dirty = self.repository / "concurrent.txt"
        dirty.write_text("not reviewed\n", encoding="utf-8")
        with self.assertRaisesRegex(ChangeApplyError, "clean"):
            require_prepared_change_source_ready(preview)
        dirty.unlink()

        preview.patch_path.unlink()
        with self.assertRaisesRegex(ChangeApplyError, "unavailable"):
            prepared_change_preview_from_dict(
                preview.to_dict(),
                expected_repository=self.repository,
                expected_base_sha=self.base_sha,
                expected_write_scope=["src"],
                expected_change_manifest_sha256=prepared.manifest_sha256,
                expected_preview_root=preview_root,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
