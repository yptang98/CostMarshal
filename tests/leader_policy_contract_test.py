from __future__ import annotations

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

from costmarshal_v2.cli import build_parser  # noqa: E402
from costmarshal_v2.routing import (  # noqa: E402
    RoutingValidationError,
    decide_route,
    default_provider_catalog,
    legacy_provider_catalog,
    validate_provider_catalog,
)
from costmarshal_v2.scheduler import (  # noqa: E402
    LEADER_PROVIDER_DEFAULT,
    normalize_leader_model,
    resolve_leader_policy,
)


CLI = ROOT / "scripts" / "costmarshal.py"


class LeaderPolicyContractTest(unittest.TestCase):
    def test_default_leader_policy_is_codex_signed_in(self) -> None:
        policy = resolve_leader_policy(
            {},
            provider=None,
            model=None,
            profile=None,
        )
        self.assertEqual(policy["provider"], LEADER_PROVIDER_DEFAULT)
        self.assertEqual(policy["model"], "inherit")
        self.assertIsNone(policy["profile"])
        self.assertEqual(policy["tier"], "high")
        self.assertTrue(policy["expert_escalation"])

    def test_deepseek_leader_derives_profile_and_env_key(self) -> None:
        policy = resolve_leader_policy(
            {"provider_catalog": validate_provider_catalog(default_provider_catalog())},
            provider="deepseek",
            model="deepseek-v4-pro",
            profile=None,
        )
        self.assertEqual(policy["provider"], "deepseek")
        self.assertEqual(policy["model"], "deepseek-v4-pro")
        self.assertEqual(policy["profile"], "deepseek")
        self.assertEqual(policy["env_key"], "DEEPSEEK_API_KEY")

    def test_leader_validation_is_fail_closed(self) -> None:
        with self.assertRaises(SystemExit):
            resolve_leader_policy({}, provider="openai", model=None, profile=None)
        with self.assertRaises(SystemExit):
            resolve_leader_policy({}, provider="deepseek", model="bad model!", profile=None)
        with self.assertRaises(SystemExit):
            resolve_leader_policy({}, provider="deepseek", model="inherit", profile="CON")
        with self.assertRaises(SystemExit):
            normalize_leader_model("$(touch /tmp/x)")

    def test_catalog_marks_codex_expert_only(self) -> None:
        catalog = validate_provider_catalog(default_provider_catalog())
        codex = next(row for row in catalog["providers"] if row["provider_id"] == "codex")
        self.assertIs(codex["expert_only"], True)
        deepseek = next(row for row in catalog["providers"] if row["provider_id"] == "deepseek")
        self.assertIs(deepseek["expert_only"], False)

    def test_expert_only_provider_is_deferred_for_routine_work(self) -> None:
        catalog = validate_provider_catalog(default_provider_catalog())
        decision = decide_route(
            {
                "task_type": "implementation",
                "risk": "low",
                "difficulty": "normal",
            },
            catalog,
        )
        self.assertEqual(decision.provider_id, "deepseek")
        self.assertEqual(decision.tier, "medium")

    def test_major_decision_raises_floor_and_enables_expert(self) -> None:
        catalog = validate_provider_catalog(default_provider_catalog())
        decision = decide_route(
            {
                "task_type": "analysis",
                "risk": "low",
                "difficulty": "simple",
                "major_decision": True,
            },
            catalog,
        )
        self.assertEqual(decision.tier_floor, "high")
        self.assertEqual(decision.provider_id, "codex")

    def test_hard_difficulty_reaches_expert_tier(self) -> None:
        catalog = validate_provider_catalog(default_provider_catalog())
        decision = decide_route(
            {
                "task_type": "implementation",
                "risk": "low",
                "difficulty": "hard",
            },
            catalog,
        )
        self.assertEqual(decision.provider_id, "codex")

    def test_explicit_expert_request_is_honored_on_low_floor(self) -> None:
        catalog = validate_provider_catalog(default_provider_catalog())
        decision = decide_route(
            {"task_type": "analysis", "risk": "low", "difficulty": "simple"},
            catalog,
            requested_provider_id="codex",
        )
        self.assertEqual(decision.provider_id, "codex")
        explicit_tier = decide_route(
            {"task_type": "analysis", "risk": "low", "difficulty": "simple"},
            catalog,
            requested_tier="high",
        )
        self.assertEqual(explicit_tier.provider_id, "codex")

    def test_legacy_catalog_falls_back_to_expert_when_required(self) -> None:
        catalog = validate_provider_catalog(legacy_provider_catalog())
        decision = decide_route(
            {"task_type": "implementation", "risk": "low", "difficulty": "normal"},
            catalog,
        )
        self.assertEqual(decision.tier_floor, "medium")
        self.assertEqual(decision.provider_id, "codex")

    def test_major_decision_type_is_strict(self) -> None:
        catalog = validate_provider_catalog(default_provider_catalog())
        with self.assertRaises(RoutingValidationError):
            decide_route(
                {"task_type": "analysis", "major_decision": "yes"},
                catalog,
            )

    def test_cli_surface_exposes_leader_and_major_decision_options(self) -> None:
        parser = build_parser()
        commands = parser._subparsers._group_actions[0].choices
        self.assertIn("configure-leader", commands)
        init_parser = commands["init"]
        self.assertIn("--leader-provider", init_parser._option_string_actions)
        self.assertIn("--leader-model", init_parser._option_string_actions)
        self.assertIn("--leader-profile", init_parser._option_string_actions)
        self.assertIn("--major-decision", commands["new-task"]._option_string_actions)
        self.assertIn("--major-decision", commands["route"]._option_string_actions)
        self.assertIn("--provider", commands["start-leader"]._option_string_actions)
        self.assertIn("--dry-run", commands["configure-leader"]._option_string_actions)

    def test_init_and_configure_leader_end_to_end(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="costmarshal-leader-policy-"))
        workspace = root / "ws"
        workspace.mkdir()
        try:
            init = run_json(
                root,
                "init",
                "--name",
                "leader-policy-e2e",
                "--objective",
                "Verify leader policy persistence",
                "--backend",
                "local",
                "--allow-unsafe-native-workers",
                "--leader-provider",
                "deepseek",
                "--leader-model",
                "deepseek-v4-pro",
                "--leader-profile",
                "deepseek",
            )
            project_dir = Path(init["project"])
            project = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
            policy = project["leader_policy"]
            self.assertEqual(policy["provider"], "deepseek")
            self.assertEqual(policy["model"], "deepseek-v4-pro")
            self.assertEqual(policy["profile"], "deepseek")
            leader = json.loads(
                (project_dir / "scheduler" / "actors" / "leader.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(leader["provider"], "deepseek")
            self.assertEqual(leader["model"], "deepseek-v4-pro")
            self.assertEqual(leader["profile"], "deepseek")
            self.assertEqual(leader["env_key"], "DEEPSEEK_API_KEY")

            plan = run_json(
                root,
                "start-leader",
                "--project",
                "leader-policy-e2e",
                "--dry-run",
            )
            self.assertEqual(plan["status"], "ok")
            self.assertTrue(plan["dry_run"])

            dry = run_json(
                root,
                "configure-leader",
                "--project",
                "leader-policy-e2e",
                "--dry-run",
                "--provider",
                "codex",
                "--model",
                "inherit",
            )
            self.assertTrue(dry["dry_run"])
            self.assertEqual(dry["policy"]["provider"], "codex")
            persisted = json.loads(
                (project_dir / "project.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["leader_policy"]["provider"], "deepseek")

            applied = run_json(
                root,
                "configure-leader",
                "--project",
                "leader-policy-e2e",
                "--provider",
                "deepseek",
                "--model",
                "deepseek-v4-pro",
            )
            self.assertFalse(applied["dry_run"])
            persisted = json.loads(
                (project_dir / "project.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["leader_policy"]["provider"], "deepseek")
            self.assertEqual(persisted["leader_policy"]["model"], "deepseek-v4-pro")
        finally:
            shutil.rmtree(root, ignore_errors=True)


def run_json(root: Path, *args: str) -> dict:
    env = os.environ.copy()
    completed = subprocess.run(
        [sys.executable, str(CLI), "--root", str(root), *args],
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"Command failed: {' '.join(args)}\nSTDOUT:\n{completed.stdout}\n"
            f"STDERR:\n{completed.stderr}"
        )
    return json.loads(completed.stdout)


if __name__ == "__main__":
    unittest.main()
