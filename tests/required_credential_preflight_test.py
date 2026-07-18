from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.paths import ProjectLayout  # noqa: E402
from costmarshal_v2.scheduler import preflight_worker_isolation  # noqa: E402


class RequiredCredentialPreflightTest(unittest.TestCase):
    def test_explicit_legacy_codex_high_without_env_key_fails_before_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-credential-preflight-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            layout = ProjectLayout(root=root / "runtime", project_dir=root / "project")
            project = {
                "project_id": "credential-fixture",
                "workspace": str(workspace),
                "worker_isolation": {},
                "provider_catalog": {
                    "schema_version": 1,
                    "providers": [
                        {
                            "provider_id": "codex",
                            "tier": "high",
                            "env_key": None,
                        }
                    ],
                },
            }
            before = copy.deepcopy(project)
            actor = {
                "id": "agent-v2-0001",
                "attempt_id": "ATT-credential",
                "provider": "codex",
                "tier": "high",
                "env_key": None,
            }
            with self.assertRaisesRegex(SystemExit, "expected CODEX_API_KEY"):
                preflight_worker_isolation(
                    layout,
                    project,
                    {"allowed_paths": []},
                    actor,
                    unsafe_native=False,
                )
            self.assertEqual(project, before)
            self.assertFalse(layout.project_dir.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
