#!/usr/bin/env python3
"""Focused contracts for safe context projections and change artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from costmarshal_v2.context_projection import (  # noqa: E402
    ChangeLimits,
    ChangeOperation,
    ContextProjectionError,
    ProjectionLimits,
    apply_cumulative_change_artifact,
    build_cumulative_change_manifest,
    capture_projection_changes,
    is_sensitive_context_path,
    materialize_context_projection,
    persist_change_artifact,
    verify_materialized_context_projection,
)


BASE_SHA = "a" * 40


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def run_git(repository: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout


def init_repository(root: Path) -> Path:
    repository = root / "repository"
    repository.mkdir()
    run_git(repository, "init", "--quiet")
    run_git(repository, "config", "user.name", "CostMarshal Test")
    run_git(repository, "config", "user.email", "costmarshal@example.invalid")
    run_git(repository, "config", "core.ignorecase", "false")
    return repository


def commit_index(repository: Path, message: str) -> str:
    run_git(repository, "commit", "--quiet", "-m", message)
    return run_git(repository, "rev-parse", "HEAD").decode("ascii").strip()


class ContextProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = Path(tempfile.mkdtemp(prefix="costmarshal-context-projection-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temporary, ignore_errors=True)

    def test_projection_uses_exact_commit_and_excludes_worktree_and_git_metadata(self) -> None:
        repository = init_repository(self.temporary)
        (repository / "src").mkdir()
        (repository / "docs").mkdir()
        (repository / "src" / "main.py").write_bytes(b"print('base')\n")
        (repository / "src" / "tool.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
        (repository / "docs" / "readme.md").write_bytes(b"not selected\n")
        (repository / ".env").write_bytes(b"TOKEN=tracked-but-sensitive\n")
        run_git(repository, "add", "--", "src", "docs", ".env")
        run_git(repository, "update-index", "--chmod=+x", "src/tool.sh")
        base_sha = commit_index(repository, "base")

        (repository / "src" / "main.py").write_bytes(b"print('mutable worktree')\n")
        (repository / "src" / "untracked.txt").write_bytes(b"must not escape\n")
        destination = self.temporary / "projection"
        projected = materialize_context_projection(
            repository,
            base_sha=base_sha,
            allowlist=["src"],
            destination=destination,
        )

        self.assertEqual((projected.files_root / "src" / "main.py").read_bytes(), b"print('base')\n")
        self.assertEqual((projected.files_root / "src" / "tool.sh").read_bytes(), b"#!/bin/sh\nexit 0\n")
        self.assertFalse((projected.files_root / "src" / "untracked.txt").exists())
        self.assertFalse((projected.files_root / "docs").exists())
        self.assertFalse((projected.files_root / ".env").exists())
        self.assertFalse(any(path.name.casefold() == ".git" for path in projected.files_root.rglob("*")))
        self.assertEqual(projected.manifest["base_sha"], base_sha)
        self.assertEqual(projected.manifest["file_count"], 2)
        modes = {entry["path"]: entry["mode"] for entry in projected.manifest["files"]}
        self.assertEqual(modes["src/tool.sh"], "100755")
        stored_manifest = json.loads(projected.manifest_path.read_text(encoding="utf-8"))
        body = dict(stored_manifest)
        observed_hash = body.pop("manifest_sha256")
        self.assertEqual(observed_hash, "sha256:" + hashlib.sha256(canonical_json_bytes(body)).hexdigest())

        repeated = materialize_context_projection(
            repository,
            base_sha=base_sha,
            allowlist=["src"],
            destination=self.temporary / "projection-repeat",
        )
        self.assertEqual(repeated.manifest, projected.manifest)

    def test_empty_projection_requires_explicit_admission_and_never_exports_the_tree(self) -> None:
        repository = init_repository(self.temporary)
        (repository / "must-not-leak.txt").write_bytes(b"repository content")
        run_git(repository, "add", "must-not-leak.txt")
        base_sha = commit_index(repository, "base")
        with self.assertRaisesRegex(ContextProjectionError, "must not be empty"):
            materialize_context_projection(
                repository,
                base_sha=base_sha,
                allowlist=[],
                destination=self.temporary / "strict-empty",
            )
        projected = materialize_context_projection(
            repository,
            base_sha=base_sha,
            allowlist=[],
            destination=self.temporary / "admitted-empty",
            allow_empty=True,
        )
        self.assertEqual(projected.manifest["allowlist"], [])
        self.assertEqual(projected.manifest["files"], [])
        self.assertEqual(list(projected.files_root.iterdir()), [])
        verified = verify_materialized_context_projection(
            projected.artifact_root,
            expected_base_sha=base_sha,
            expected_allowlist=[],
        )
        self.assertEqual(verified.manifest["file_count"], 0)
        with self.assertRaisesRegex(ContextProjectionError, "boolean"):
            materialize_context_projection(
                repository,
                base_sha=base_sha,
                allowlist=[],
                destination=self.temporary / "invalid-empty-flag",
                allow_empty=1,  # type: ignore[arg-type]
            )

    def test_verifier_rejects_binding_manifest_content_and_tree_drift(self) -> None:
        repository = init_repository(self.temporary)
        (repository / "src").mkdir()
        (repository / "src" / "main.py").write_bytes(b"base\n")
        run_git(repository, "add", "src/main.py")
        base_sha = commit_index(repository, "base")
        projected = materialize_context_projection(
            repository,
            base_sha=base_sha,
            allowlist=["src"],
            destination=self.temporary / "verify",
        )
        verify_materialized_context_projection(
            projected.artifact_root,
            expected_base_sha=base_sha,
            expected_allowlist=["src"],
            expected_manifest_sha256=projected.manifest["manifest_sha256"],
        )
        with self.assertRaisesRegex(ContextProjectionError, "immutable identity"):
            verify_materialized_context_projection(
                projected.artifact_root,
                expected_base_sha=base_sha,
                expected_allowlist=["src"],
                expected_manifest_sha256="sha256:" + ("0" * 64),
            )
        with self.assertRaisesRegex(ContextProjectionError, "expected task base"):
            verify_materialized_context_projection(
                projected.artifact_root,
                expected_base_sha="b" * 40,
                expected_allowlist=["src"],
            )
        with self.assertRaisesRegex(ContextProjectionError, "expected task context"):
            verify_materialized_context_projection(
                projected.artifact_root,
                expected_base_sha=base_sha,
                expected_allowlist=["src/main.py"],
            )

        projected_file = projected.files_root / "src" / "main.py"
        projected_file.write_bytes(b"drift\n")
        with self.assertRaisesRegex(ContextProjectionError, "content identity drifted"):
            verify_materialized_context_projection(
                projected.artifact_root,
                expected_base_sha=base_sha,
                expected_allowlist=["src"],
            )
        projected_file.write_bytes(b"base\n")
        if os.name != "nt":
            projected_file.chmod(0o600)
            with self.assertRaisesRegex(ContextProjectionError, "permission mode drifted"):
                verify_materialized_context_projection(
                    projected.artifact_root,
                    expected_base_sha=base_sha,
                    expected_allowlist=["src"],
                )
            projected_file.chmod(0o644)
        extra = projected.files_root / "src" / "extra.txt"
        extra.write_bytes(b"extra")
        with self.assertRaisesRegex(ContextProjectionError, "file set drifted"):
            verify_materialized_context_projection(
                projected.artifact_root,
                expected_base_sha=base_sha,
                expected_allowlist=["src"],
            )
        extra.unlink()
        empty_directory = projected.files_root / "empty"
        empty_directory.mkdir()
        with self.assertRaisesRegex(ContextProjectionError, "directory set drifted"):
            verify_materialized_context_projection(
                projected.artifact_root,
                expected_base_sha=base_sha,
                expected_allowlist=["src"],
            )
        empty_directory.rmdir()
        canonical_manifest = projected.manifest_path.read_bytes()
        parsed = json.loads(canonical_manifest)
        projected.manifest_path.write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ContextProjectionError, "bytes are not canonical"):
            verify_materialized_context_projection(
                projected.artifact_root,
                expected_base_sha=base_sha,
                expected_allowlist=["src"],
            )
        projected.manifest_path.write_bytes(canonical_manifest)
        (projected.artifact_root / "unexpected").write_bytes(b"extra root file")
        with self.assertRaisesRegex(ContextProjectionError, "root layout is invalid"):
            verify_materialized_context_projection(
                projected.artifact_root,
                expected_base_sha=base_sha,
                expected_allowlist=["src"],
            )

    def test_projection_rejects_invalid_sensitive_missing_and_existing_destinations(self) -> None:
        repository = init_repository(self.temporary)
        (repository / "safe.txt").write_bytes(b"safe")
        (repository / ".env").write_bytes(b"SECRET=value")
        run_git(repository, "add", "--", "safe.txt", ".env")
        base_sha = commit_index(repository, "base")

        for allowlist in (["../safe.txt"], [".git/config"], [".env"], ["missing.txt"]):
            with self.subTest(allowlist=allowlist):
                with self.assertRaises(ContextProjectionError):
                    materialize_context_projection(
                        repository,
                        base_sha=base_sha,
                        allowlist=allowlist,
                        destination=self.temporary
                        / ("out-" + hashlib.sha256(repr(allowlist).encode()).hexdigest()[:8]),
                    )
        with self.assertRaises(ContextProjectionError):
            materialize_context_projection(
                repository,
                base_sha="HEAD",
                allowlist=["safe.txt"],
                destination=self.temporary / "symbolic-base",
            )
        existing = self.temporary / "existing"
        existing.mkdir()
        with self.assertRaises(ContextProjectionError):
            materialize_context_projection(
                repository,
                base_sha=base_sha,
                allowlist=["safe.txt"],
                destination=existing,
            )

    def test_projection_rejects_git_symlink_and_submodule_entries(self) -> None:
        repository = init_repository(self.temporary)
        (repository / "ordinary.txt").write_bytes(b"ordinary")
        run_git(repository, "add", "ordinary.txt")
        initial_sha = commit_index(repository, "initial")
        target_blob = run_git(
            repository, "hash-object", "-w", "--stdin", input_bytes=b"ordinary.txt"
        ).decode("ascii").strip()
        run_git(repository, "update-index", "--add", "--cacheinfo", "120000", target_blob, "linked")
        symlink_sha = commit_index(repository, "synthetic symlink")
        with self.assertRaisesRegex(ContextProjectionError, "symlink"):
            materialize_context_projection(
                repository,
                base_sha=symlink_sha,
                allowlist=["linked"],
                destination=self.temporary / "symlink-output",
            )

        run_git(repository, "rm", "--quiet", "--cached", "linked")
        run_git(repository, "update-index", "--add", "--cacheinfo", "160000", initial_sha, "vendor")
        submodule_sha = commit_index(repository, "synthetic submodule")
        with self.assertRaisesRegex(ContextProjectionError, "submodule"):
            materialize_context_projection(
                repository,
                base_sha=submodule_sha,
                allowlist=["vendor"],
                destination=self.temporary / "submodule-output",
            )

    def test_projection_rejects_case_collisions_and_resource_overruns(self) -> None:
        repository = init_repository(self.temporary)
        blob_one = run_git(repository, "hash-object", "-w", "--stdin", input_bytes=b"one").decode("ascii").strip()
        blob_two = run_git(repository, "hash-object", "-w", "--stdin", input_bytes=b"two").decode("ascii").strip()
        run_git(repository, "update-index", "--add", "--cacheinfo", "100644", blob_one, "pkg/A.txt")
        run_git(repository, "update-index", "--add", "--cacheinfo", "100644", blob_two, "pkg/a.txt")
        base_sha = commit_index(repository, "case collision")
        with self.assertRaisesRegex(ContextProjectionError, "collision"):
            materialize_context_projection(
                repository,
                base_sha=base_sha,
                allowlist=["pkg"],
                destination=self.temporary / "case-output",
            )
        with self.assertRaisesRegex(ContextProjectionError, "exceeding 1"):
            materialize_context_projection(
                repository,
                base_sha=base_sha,
                allowlist=["pkg"],
                destination=self.temporary / "count-output",
                limits=ProjectionLimits(max_files=1),
            )
        with self.assertRaisesRegex(ContextProjectionError, "exceeds 2 bytes"):
            materialize_context_projection(
                repository,
                base_sha=base_sha,
                allowlist=["pkg/A.txt"],
                destination=self.temporary / "size-output",
                limits=ProjectionLimits(max_file_bytes=2),
            )
        with self.assertRaisesRegex(ContextProjectionError, "output exceeded 1 bytes"):
            materialize_context_projection(
                repository,
                base_sha=base_sha,
                allowlist=["pkg/A.txt"],
                destination=self.temporary / "metadata-output",
                limits=ProjectionLimits(max_git_metadata_bytes=1),
            )

    def test_projection_rejects_symlinked_destination_ancestor(self) -> None:
        repository = init_repository(self.temporary)
        (repository / "safe.txt").write_bytes(b"safe")
        run_git(repository, "add", "safe.txt")
        base_sha = commit_index(repository, "base")
        real = self.temporary / "real"
        real.mkdir()
        linked = self.temporary / "linked"
        try:
            linked.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this host")
        with self.assertRaisesRegex(ContextProjectionError, "symlink/reparse"):
            materialize_context_projection(
                repository,
                base_sha=base_sha,
                allowlist=["safe.txt"],
                destination=linked / "projection",
            )


class CumulativeChangeArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = Path(tempfile.mkdtemp(prefix="costmarshal-change-artifact-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temporary, ignore_errors=True)

    def test_cumulative_changes_are_base_relative_and_content_addressed(self) -> None:
        first = build_cumulative_change_manifest(
            base_sha=BASE_SHA,
            write_scope=["src"],
            operations=[
                ChangeOperation.upsert("src/a.txt", b"A"),
                ChangeOperation.upsert("src/b.sh", b"B", executable=True),
                ChangeOperation.delete("src/old.txt"),
            ],
        )
        first_persisted = persist_change_artifact(self.temporary / "cas", first)
        first_body = first_persisted.manifest_path.read_bytes()
        self.assertEqual(
            first_persisted.manifest_sha256,
            "sha256:" + hashlib.sha256(first_body).hexdigest(),
        )
        self.assertNotIn("manifest_sha256", json.loads(first_body))
        self.assertEqual(len(first_persisted.blob_paths), 2)

        second = build_cumulative_change_manifest(
            base_sha=BASE_SHA,
            write_scope=["src"],
            previous=first,
            operations=[
                ChangeOperation.delete("src/a.txt"),
                ChangeOperation.upsert("src/c.txt", b"CCC"),
            ],
        )
        entries = {entry["path"]: entry for entry in second.manifest["changes"]}
        self.assertEqual(entries["src/a.txt"]["operation"], "delete")
        self.assertEqual(entries["src/b.sh"]["operation"], "upsert")
        self.assertEqual(entries["src/c.txt"]["operation"], "upsert")
        self.assertEqual(entries["src/old.txt"]["operation"], "delete")
        second_persisted = persist_change_artifact(self.temporary / "cas", second)
        self.assertTrue(second_persisted.manifest_path.is_file())
        repeated = persist_change_artifact(self.temporary / "cas", second)
        self.assertEqual(repeated, second_persisted)

        b_digest = entries["src/b.sh"]["blob_sha256"].removeprefix("sha256:")
        b_path = self.temporary / "cas" / "blobs" / "sha256" / b_digest
        b_path.write_bytes(b"X")
        with self.assertRaisesRegex(ContextProjectionError, "does not match manifest"):
            persist_change_artifact(self.temporary / "cas", second)

    def test_capture_detects_add_modify_delete_and_rebuilds_previous_state(self) -> None:
        repository = init_repository(self.temporary)
        (repository / "src").mkdir()
        (repository / "docs").mkdir()
        (repository / "src" / "modify.txt").write_bytes(b"base modify\n")
        (repository / "src" / "delete.txt").write_bytes(b"base delete\n")
        (repository / "docs" / "outside.txt").write_bytes(b"must remain\n")
        run_git(repository, "add", "src", "docs")
        base_sha = commit_index(repository, "base")
        projected = materialize_context_projection(
            repository,
            base_sha=base_sha,
            allowlist=["src", "docs"],
            destination=self.temporary / "capture",
        )
        (projected.files_root / "src" / "modify.txt").write_bytes(b"worker edit\n")
        (projected.files_root / "src" / "delete.txt").unlink()
        (projected.files_root / "src" / "new.txt").write_bytes(b"worker new\n")
        captured = capture_projection_changes(
            projected.artifact_root,
            expected_base_sha=base_sha,
            expected_allowlist=["src", "docs"],
            write_scope=["src"],
            expected_manifest_sha256=projected.manifest["manifest_sha256"],
        )
        changes = {entry["path"]: entry for entry in captured.manifest["changes"]}
        self.assertEqual(changes["src/delete.txt"]["operation"], "delete")
        self.assertEqual(changes["src/modify.txt"]["operation"], "upsert")
        self.assertEqual(changes["src/new.txt"]["operation"], "upsert")
        self.assertEqual(len(captured.blobs), 2)
        persisted = persist_change_artifact(self.temporary / "captured-cas", captured)
        self.assertTrue(persisted.manifest_path.is_file())

        (projected.files_root / "docs" / "outside.txt").write_bytes(b"forbidden edit\n")
        with self.assertRaisesRegex(ContextProjectionError, "outside write scope"):
            capture_projection_changes(
                projected.artifact_root,
                expected_base_sha=base_sha,
                expected_allowlist=["src", "docs"],
                write_scope=["src"],
            )

        reset_projection = materialize_context_projection(
            repository,
            base_sha=base_sha,
            allowlist=["src", "docs"],
            destination=self.temporary / "capture-reset",
        )
        reset = capture_projection_changes(
            reset_projection.artifact_root,
            expected_base_sha=base_sha,
            expected_allowlist=["src", "docs"],
            write_scope=["src"],
            previous=captured,
        )
        self.assertEqual(reset.manifest["changes"], [])
        self.assertEqual(reset.blobs, ())

    def test_capture_rejects_sensitive_links_limits_and_ambiguous_previous_state(self) -> None:
        repository = init_repository(self.temporary)
        (repository / "src").mkdir()
        (repository / "src" / "base.txt").write_bytes(b"base")
        run_git(repository, "add", "src/base.txt")
        base_sha = commit_index(repository, "base")
        projected = materialize_context_projection(
            repository,
            base_sha=base_sha,
            allowlist=["src"],
            destination=self.temporary / "capture-unsafe",
        )
        (projected.files_root / "src" / ".env").write_bytes(b"TOKEN=secret")
        with self.assertRaisesRegex(ContextProjectionError, "sensitive"):
            capture_projection_changes(
                projected.artifact_root,
                expected_base_sha=base_sha,
                expected_allowlist=["src"],
                write_scope=["src"],
            )
        (projected.files_root / "src" / ".env").unlink()
        (projected.files_root / "src" / "base.txt").write_bytes(b"too large")
        with self.assertRaisesRegex(ContextProjectionError, "size is invalid"):
            capture_projection_changes(
                projected.artifact_root,
                expected_base_sha=base_sha,
                expected_allowlist=["src"],
                write_scope=["src"],
                change_limits=ChangeLimits(max_blob_bytes=2),
            )

        ambiguous_previous = build_cumulative_change_manifest(
            base_sha=base_sha,
            write_scope=["src", "docs"],
            operations=[ChangeOperation.upsert("docs/unobserved.txt", b"prior")],
        )
        clean_projection = materialize_context_projection(
            repository,
            base_sha=base_sha,
            allowlist=["src"],
            destination=self.temporary / "capture-ambiguous",
        )
        with self.assertRaisesRegex(ContextProjectionError, "outside the observable context"):
            capture_projection_changes(
                clean_projection.artifact_root,
                expected_base_sha=base_sha,
                expected_allowlist=["src"],
                write_scope=["src", "docs"],
                previous=ambiguous_previous,
            )

        link_projection = materialize_context_projection(
            repository,
            base_sha=base_sha,
            allowlist=["src"],
            destination=self.temporary / "capture-link",
        )
        link = link_projection.files_root / "src" / "linked"
        try:
            link.symlink_to(link_projection.files_root / "src" / "base.txt")
        except (OSError, NotImplementedError):
            pass
        else:
            with self.assertRaisesRegex(ContextProjectionError, "symlink/reparse"):
                capture_projection_changes(
                    link_projection.artifact_root,
                    expected_base_sha=base_sha,
                    expected_allowlist=["src"],
                    write_scope=["src"],
                )

    def test_empty_projection_can_capture_only_declared_new_files(self) -> None:
        repository = init_repository(self.temporary)
        (repository / "hidden.txt").write_bytes(b"not projected")
        run_git(repository, "add", "hidden.txt")
        base_sha = commit_index(repository, "base")
        projected = materialize_context_projection(
            repository,
            base_sha=base_sha,
            allowlist=[],
            destination=self.temporary / "capture-empty",
            allow_empty=True,
        )
        output = projected.files_root / "out" / "result.txt"
        output.parent.mkdir()
        output.write_bytes(b"created")
        captured = capture_projection_changes(
            projected.artifact_root,
            expected_base_sha=base_sha,
            expected_allowlist=[],
            write_scope=["out"],
        )
        self.assertEqual(captured.manifest["change_count"], 1)
        self.assertEqual(captured.manifest["changes"][0]["path"], "out/result.txt")
        self.assertEqual(captured.blobs[0][1], b"created")
        with self.assertRaisesRegex(ContextProjectionError, "outside write scope"):
            capture_projection_changes(
                projected.artifact_root,
                expected_base_sha=base_sha,
                expected_allowlist=[],
                write_scope=["different"],
            )

    def test_cumulative_artifact_apply_is_idempotent_and_fails_on_unrelated_drift(self) -> None:
        repository = init_repository(self.temporary)
        (repository / "src").mkdir()
        (repository / "src" / "app.py").write_bytes(b"base\n")
        (repository / "src" / "old.txt").write_bytes(b"old\n")
        run_git(repository, "add", "src")
        base_sha = commit_index(repository, "base")
        projection = materialize_context_projection(
            repository,
            base_sha=base_sha,
            allowlist=["src"],
            destination=self.temporary / "apply-projection",
        )
        changes = build_cumulative_change_manifest(
            base_sha=base_sha,
            write_scope=["src", "out"],
            operations=[
                ChangeOperation.upsert("src/app.py", b"changed\n"),
                ChangeOperation.delete("src/old.txt"),
                ChangeOperation.upsert("out/new.txt", b"new\n"),
            ],
        )
        persisted = persist_change_artifact(self.temporary / "apply-cas", changes)
        arguments = {
            "expected_base_sha": base_sha,
            "expected_allowlist": ["src"],
            "expected_projection_manifest_sha256": projection.manifest["manifest_sha256"],
            "write_scope": ["src", "out"],
            "change_manifest": changes.manifest,
            "change_artifact_root": persisted.artifact_root,
            "expected_change_manifest_sha256": persisted.manifest_sha256,
        }
        apply_cumulative_change_artifact(projection.artifact_root, **arguments)
        self.assertEqual((projection.files_root / "src" / "app.py").read_bytes(), b"changed\n")
        self.assertFalse((projection.files_root / "src" / "old.txt").exists())
        self.assertEqual((projection.files_root / "out" / "new.txt").read_bytes(), b"new\n")

        (projection.files_root / "src" / "app.py").write_bytes(b"partial\n")
        apply_cumulative_change_artifact(projection.artifact_root, **arguments)
        self.assertEqual((projection.files_root / "src" / "app.py").read_bytes(), b"changed\n")
        self.assertEqual((repository / "src" / "app.py").read_bytes(), b"base\n")

        (projection.files_root / "out" / "extra.txt").write_bytes(b"unbound\n")
        with self.assertRaisesRegex(ContextProjectionError, "does not match"):
            apply_cumulative_change_artifact(projection.artifact_root, **arguments)

    def test_change_builder_enforces_paths_scope_collisions_and_limits(self) -> None:
        invalid_operations = (
            ChangeOperation.upsert("../escape", b"x"),
            ChangeOperation.upsert(".git/config", b"x"),
            ChangeOperation.upsert("src/.env", b"x"),
            ChangeOperation.upsert("docs/outside.txt", b"x"),
        )
        for operation in invalid_operations:
            with self.subTest(path=operation.path):
                with self.assertRaises(ContextProjectionError):
                    build_cumulative_change_manifest(
                        base_sha=BASE_SHA,
                        write_scope=["src"],
                        operations=[operation],
                    )
        with self.assertRaisesRegex(ContextProjectionError, "collide"):
            build_cumulative_change_manifest(
                base_sha=BASE_SHA,
                write_scope=["src"],
                operations=[
                    ChangeOperation.upsert("src/A.txt", b"a"),
                    ChangeOperation.upsert("src/a.txt", b"b"),
                ],
            )
        with self.assertRaisesRegex(ContextProjectionError, "file/directory collision"):
            build_cumulative_change_manifest(
                base_sha=BASE_SHA,
                write_scope=["src"],
                operations=[
                    ChangeOperation.upsert("src/a", b"a"),
                    ChangeOperation.upsert("src/a/b", b"b"),
                    ChangeOperation.upsert("src/a-x", b"x"),
                ],
            )
        with self.assertRaisesRegex(ContextProjectionError, "exceeds 2 bytes"):
            build_cumulative_change_manifest(
                base_sha=BASE_SHA,
                write_scope=["src"],
                operations=[ChangeOperation.upsert("src/large", b"123")],
                limits=ChangeLimits(max_blob_bytes=2),
            )
        overwritten = build_cumulative_change_manifest(
            base_sha=BASE_SHA,
            write_scope=["src"],
            operations=[
                ChangeOperation.upsert("src/value", b"old"),
                ChangeOperation.upsert("src/value", b"new"),
            ],
        )
        self.assertEqual(len(overwritten.blobs), 1)
        self.assertEqual(overwritten.blobs[0][1], b"new")

    def test_previous_manifest_is_hash_base_and_scope_bound(self) -> None:
        first = build_cumulative_change_manifest(
            base_sha=BASE_SHA,
            write_scope=["src"],
            operations=[ChangeOperation.upsert("src/a", b"a")],
        )
        tampered = dict(first.manifest)
        tampered["change_count"] = 99
        with self.assertRaisesRegex(ContextProjectionError, "hash"):
            build_cumulative_change_manifest(
                base_sha=BASE_SHA,
                write_scope=["src"],
                previous=tampered,
                operations=[],
            )
        with self.assertRaisesRegex(ContextProjectionError, "different base_sha"):
            build_cumulative_change_manifest(
                base_sha="b" * 40,
                write_scope=["src"],
                previous=first,
                operations=[],
            )
        with self.assertRaisesRegex(ContextProjectionError, "different write scope"):
            build_cumulative_change_manifest(
                base_sha=BASE_SHA,
                write_scope=["src", "docs"],
                previous=first,
                operations=[],
            )

    def test_sensitive_path_classifier_is_fail_closed(self) -> None:
        for path in (
            ".env",
            "config/.env.production",
            ".ssh/id_rsa",
            "deploy/private.pem",
            ".docker/config.json",
            ".git/config",
        ):
            self.assertTrue(is_sensitive_context_path(path), path)
        self.assertFalse(is_sensitive_context_path("src/environment.py"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
