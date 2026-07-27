"""Repository CI is a least-privilege, cross-platform release contract."""

from __future__ import annotations

import re
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


class CiContractTest(unittest.TestCase):
    def test_ci_runs_the_complete_evidence_suite_on_supported_boundaries(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("pull_request:", text)
        self.assertIn("workflow_dispatch:", text)
        self.assertRegex(text, r"branches:\s+- v2\s+- v3\s+- v4")
        self.assertIn("ubuntu-latest", text)
        self.assertIn("windows-latest", text)
        self.assertIn('          - "3.11"', text)
        self.assertIn('          - "3.13"', text)
        self.assertNotIn('          - "3.10"', text)
        self.assertIn("python tests/release/run_local_test_evidence.py", text)
        self.assertIn("python scripts/sync_plugin_package.py", text)
        self.assertIn("sudo apt-get install --yes --no-install-recommends tmux", text)
        self.assertIn("if: runner.os == 'Linux'", text)
        self.assertIn("artifacts/local-test-report.json", text)
        self.assertIn("if-no-files-found: error", text)

    def test_actions_are_sha_pinned_and_job_has_read_only_repository_access(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertRegex(text, r"permissions:\s+contents: read")
        uses = re.findall(r"^\s*uses:\s*([^\s#]+)", text, flags=re.MULTILINE)
        self.assertTrue(uses)
        for action in uses:
            self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")
        self.assertIn("persist-credentials: false", text)
        self.assertNotIn("secrets.", text)

    def test_production_image_workflow_is_manual_immutable_and_digest_receipted(self) -> None:
        workflow = ROOT / ".github" / "workflows" / "production-images.yml"
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("packages: write", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("refs/heads/v4", text)
        self.assertIn('refs/tags/$release_version', text)
        self.assertNotIn('test "$(cat VERSION)" = "v', text)
        self.assertIn("@sha256:[0-9a-f]{64}", text)
        self.assertIn("--provenance=mode=max", text)
        self.assertIn("--sbom=true", text)
        self.assertEqual(text.count("--push"), 2)
        self.assertIn("production-images.json", text)
        self.assertIn("source_commit", text)
        self.assertIn("gateway_image", text)
        self.assertIn("worker_image", text)
        uses = re.findall(r"^\s*uses:\s*([^\s#]+)", text, flags=re.MULTILINE)
        self.assertTrue(uses)
        for action in uses:
            self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")

    def test_gateway_build_context_is_allowlisted(self) -> None:
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertTrue(dockerignore.startswith("*\n"))
        self.assertIn("!costmarshal_v2/*.py", dockerignore)
        self.assertNotIn("!costmarshal_v2/**", dockerignore)
        self.assertIn("!scripts/costmarshal_gateway.py", dockerignore)
        self.assertNotIn("!.git", dockerignore)
        self.assertNotIn("!artifacts", dockerignore)

    def test_repository_only_metadata_is_not_copied_into_skill_install(self) -> None:
        installer = (ROOT / "scripts" / "install_smoke_test.py").read_text(
            encoding="utf-8"
        )
        install_prompt = (ROOT / "INSTALL_PROMPT.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        cli = (ROOT / "scripts" / "costmarshal.py").read_text(encoding="utf-8")
        self.assertIn('".github"', installer)
        self.assertIn(".github", install_prompt)
        self.assertIn("Python 3.11+", install_prompt)
        self.assertIn("py -3.11 --version", install_prompt)
        self.assertIn("Python 3.11+", readme)
        self.assertIn("Python 3.11+", requirements)
        self.assertIn("MINIMUM_PYTHON = (3, 11)", cli)
        self.assertNotIn("Python 3.10+", install_prompt)
        self.assertNotIn("Python 3.10+", readme)
        self.assertNotIn("Python 3.10+", requirements)


if __name__ == "__main__":
    unittest.main(verbosity=2)
