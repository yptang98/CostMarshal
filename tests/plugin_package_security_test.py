#!/usr/bin/env python3
"""Adversarial contracts for the committed Codex plugin snapshot builder."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import sync_plugin_package as sync  # noqa: E402


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@contextmanager
def isolated_builder():
    with tempfile.TemporaryDirectory(prefix="costmarshal-package-security-") as raw:
        root = Path(raw) / "source"
        shutil.copytree(sync.PACKAGE, root)
        package_parent = root / "plugins"
        package_parent.mkdir()
        package = package_parent / "costmarshal"
        shutil.copytree(sync.PACKAGE, package)
        with mock.patch.multiple(
            sync,
            ROOT=root,
            PACKAGE_PARENT=package_parent,
            PACKAGE=package,
        ):
            yield root, package_parent, package


class PluginPackageSecurityTest(unittest.TestCase):
    def test_cache_and_bytecode_filters_are_case_insensitive(self) -> None:
        self.assertFalse(sync._admitted(Path("__PYCACHE__/payload.PYC")))
        self.assertFalse(sync._admitted(Path("cache/payload.PyO")))
        self.assertTrue(sync._forbidden(Path("__PYCACHE__/payload.PYC")))

    def test_windows_ambiguous_paths_are_rejected_everywhere(self) -> None:
        for relative in (
            "aux.txt",
            "nested/NUL.json",
            "trailing-dot.",
            "trailing-space ",
            "alternate:data",
            "wild*card",
        ):
            with self.subTest(relative=relative):
                self.assertFalse(sync._portable(Path(relative)))

    def test_verifier_reports_forbidden_empty_and_oversize_entries(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-package-invalid-") as raw:
            package = Path(raw) / "costmarshal"
            shutil.copytree(sync.PACKAGE, package)
            (package / ".ENV").write_text("TOKEN=do-not-package\n", encoding="utf-8")
            (package / "UNLISTED").mkdir()
            (package / "oversize.bin").write_bytes(b"x" * (sync.MAX_FILE_BYTES + 1))
            differences = sync.package_differences(package)
        self.assertIn("forbidden:.ENV", differences)
        self.assertIn("unexpected-directory:UNLISTED", differences)
        self.assertTrue(any(row.startswith("file-size:oversize.bin:") for row in differences))

    def test_unlisted_secret_cannot_replace_last_known_good_package(self) -> None:
        with isolated_builder() as (root, _parent, package):
            before = tree_digest(package)
            (root / "costmarshal_v2" / ".npmrc").write_text(
                "//registry.example/:_authToken=not-a-real-token\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "forbidden path|not explicitly admitted",
            ):
                sync.write_package()
            self.assertEqual(tree_digest(package), before)
            self.assertFalse((package / "costmarshal_v2" / ".npmrc").exists())

    def test_interrupted_backup_is_recovered_before_rebuild(self) -> None:
        with isolated_builder() as (_root, parent, package):
            backup = parent / ".costmarshal-backup"
            package.replace(backup)
            self.assertFalse(package.exists())
            sync.write_package()
            self.assertEqual(sync.package_differences(package), [])
            self.assertFalse(backup.exists())
            self.assertEqual(list(parent.glob(".costmarshal-package-*")), [])

    def test_poisoned_interrupted_backup_is_never_promoted(self) -> None:
        with isolated_builder() as (root, parent, package):
            backup = parent / ".costmarshal-backup"
            package.replace(backup)
            poisoned = backup / "scripts" / "costmarshal.py"
            poisoned.write_text("MALICIOUS_BACKUP_SENTINEL\n", encoding="utf-8")
            (root / "costmarshal_v2" / ".npmrc").write_text(
                "//registry.example/:_authToken=not-a-real-token\n",
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                sync.write_package()
            self.assertFalse(package.exists())
            self.assertTrue(backup.exists())
            self.assertEqual(
                poisoned.read_text(encoding="utf-8"),
                "MALICIOUS_BACKUP_SENTINEL\n",
            )

    def test_redirected_package_parent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-package-link-") as raw:
            base = Path(raw)
            root = base / "source"
            outside = base / "outside"
            shutil.copytree(sync.PACKAGE, root)
            outside.mkdir()
            try:
                (root / "plugins").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")
            package_parent = root / "plugins"
            package = package_parent / "costmarshal"
            with mock.patch.multiple(
                sync,
                ROOT=root,
                PACKAGE_PARENT=package_parent,
                PACKAGE=package,
            ):
                with self.assertRaisesRegex(RuntimeError, "linked or redirected"):
                    sync.write_package()
            self.assertFalse((outside / "costmarshal").exists())

    def test_redirected_source_parent_is_rejected(self) -> None:
        with isolated_builder() as (root, _parent, _package):
            scripts = root / "scripts"
            outside = root.parent / "outside-scripts"
            shutil.copytree(scripts, outside)
            shutil.rmtree(scripts)
            linked = False
            try:
                if os.name == "nt":
                    completed = subprocess.run(
                        ["cmd.exe", "/d", "/c", "mklink", "/J", str(scripts), str(outside)],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                    if completed.returncode != 0:
                        self.skipTest(f"directory junction unavailable: {completed.stderr}")
                else:
                    scripts.symlink_to(outside, target_is_directory=True)
                linked = True
                with self.assertRaisesRegex(RuntimeError, "linked or redirected"):
                    sync.write_package()
            finally:
                if linked:
                    if os.name == "nt":
                        os.rmdir(scripts)
                    else:
                        scripts.unlink()


if __name__ == "__main__":
    unittest.main()
