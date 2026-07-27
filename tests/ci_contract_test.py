"""Repository CI is a least-privilege, cross-platform release contract."""

from __future__ import annotations

from datetime import date
import json
import re
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validate_production_build_inputs import BuildInputError, validate  # noqa: E402


WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
BUILD_INPUTS = ROOT / "release" / "production-build-inputs.json"


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
        self.assertIn("linux-amd64 production container packaging", text)
        self.assertIn("scripts/validate_production_build_inputs.py", text)
        self.assertIn("container/gateway/Dockerfile", text)
        self.assertIn("container/worker/Dockerfile", text)
        self.assertIn("costmarshal-isolation-canary", text)
        self.assertIn("container-packaging-report.json", text)
        self.assertIn("Verify reviewed linux-amd64 base manifests", text)
        self.assertIn("docker buildx imagetools inspect --raw", text)
        self.assertIn("npm ci --prefix container/worker", text)
        self.assertNotIn("npm install --global @openai/codex", text)
        self.assertIn('test "$observed_python" = "$PYTHON_VERSION"', text)
        self.assertIn('test "$observed_node" = "v$NODE_VERSION"', text)

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
        self.assertIn("validate_production_build_inputs.py", text)
        self.assertIn("--provenance=mode=max", text)
        self.assertIn("--sbom=true", text)
        self.assertEqual(text.count("--push"), 2)
        self.assertIn("production-images.json", text)
        self.assertIn("source_commit", text)
        self.assertIn("gateway_image", text)
        self.assertIn("worker_image", text)
        self.assertIn("source_sha", text)
        self.assertIn('test "$SOURCE_SHA" = "$GITHUB_SHA"', text)
        self.assertIn("scripts/validate_production_build_inputs.py", text)
        self.assertIn("build_inputs_sha256", text)
        self.assertIn("python_linux_amd64_manifest", text)
        self.assertIn("node_linux_amd64_manifest", text)
        self.assertIn("codex_npm_integrity", text)
        self.assertIn("Verify reviewed linux-amd64 base manifests", text)
        self.assertIn("docker buildx imagetools inspect --raw", text)
        self.assertIn("package-lock.json", text)
        self.assertIn("npm ci --prefix container/worker", text)
        self.assertNotIn("npm install --global", text)
        self.assertIn("observed_python_version", text)
        self.assertIn("observed_node_version", text)
        self.assertIn("observed_codex_version", text)
        self.assertNotIn("inputs.python_base_image", text)
        self.assertNotIn("inputs.node_base_image", text)
        self.assertNotIn("inputs.codex_npm_version", text)
        uses = re.findall(r"^\s*uses:\s*([^\s#]+)", text, flags=re.MULTILINE)
        self.assertTrue(uses)
        for action in uses:
            self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")

    def test_production_build_inputs_are_pinned_reviewed_and_expiring(self) -> None:
        result = validate(BUILD_INPUTS, today=date(2026, 7, 28))
        self.assertEqual(result["platforms"], ["linux/amd64"])
        self.assertRegex(
            result["python_base_image"],
            r"^python:[^@]+@sha256:[0-9a-f]{64}$",
        )
        self.assertRegex(
            result["node_base_image"],
            r"^node:[^@]+@sha256:[0-9a-f]{64}$",
        )
        self.assertEqual(result["python_version"], "3.11.15")
        self.assertEqual(result["node_version"], "22.23.1")
        self.assertEqual(result["codex_npm_version"], "0.144.1")
        self.assertRegex(
            result["codex_npm_lock_sha256"],
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertRegex(
            result["build_inputs_sha256"],
            r"^sha256:[0-9a-f]{64}$",
        )
        original = json.loads(BUILD_INPUTS.read_text(encoding="utf-8"))
        mutations = (
            {"python_base_image": "python:latest"},
            {"review_expires_on": "2026-07-27"},
            {"review_expires_on": "2027-07-28"},
            {"codex_npm_integrity": "sha512-not-base64"},
            {"codex_npm_lock_sha256": "sha256:" + "0" * 64},
            {"platforms": ["linux/amd64", "linux/arm64"]},
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "inputs.json"
            for mutation in mutations:
                with self.subTest(mutation=mutation):
                    value = {**original, **mutation}
                    path.write_text(
                        json.dumps(value),
                        encoding="utf-8",
                    )
                    with self.assertRaises(BuildInputError):
                        validate(path, today=date(2026, 7, 28))

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
