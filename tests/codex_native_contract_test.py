from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from costmarshal_v2.cli import build_parser  # noqa: E402
from costmarshal_v2.codex_native import (  # noqa: E402
    CODEX_NATIVE_SCHEMA,
    CommandResult,
    codex_native_overrides,
    default_policy,
    normalized_policy,
    parse_version,
    probe_codex,
    prompt_contract,
    uses_default_codex_command,
)


class CodexNativeContractTest(unittest.TestCase):
    def actor(self) -> dict[str, object]:
        return {
            "id": "agent-low-1",
            "role": "agent",
            "runner": {"codex_native": default_policy()},
        }

    def test_attempt_local_policy_is_bounded(self) -> None:
        actor = self.actor()
        policy = normalized_policy(actor)
        self.assertEqual(policy["schema_version"], CODEX_NATIVE_SCHEMA)
        self.assertEqual(policy["mode"], "attempt-local")
        self.assertEqual(policy["max_depth"], 1)
        self.assertFalse(policy["cross_provider_children"])
        self.assertEqual(
            codex_native_overrides(actor),
            [
                "-c",
                "features.multi_agent=true",
                "-c",
                "agents.max_concurrent_threads_per_session=4",
            ],
        )
        contract = "\n".join(prompt_contract(actor))
        self.assertIn("inside this attempt's model, provider credential", contract)
        self.assertIn("must not route to another provider", contract)
        self.assertIn("single final CostMarshal report", contract)

    def test_policy_rejects_authority_and_provider_escape(self) -> None:
        for changed in (
            {"authority": "codex"},
            {"cross_provider_children": True},
            {"max_depth": 2},
            {"max_subagents": 9},
        ):
            actor = self.actor()
            actor["runner"]["codex_native"].update(changed)  # type: ignore[index,union-attr]
            with self.assertRaises(ValueError):
                normalized_policy(actor)

    def test_probe_reports_exec_and_app_server_independently(self) -> None:
        def runner(argv: list[str], timeout: float) -> CommandResult:
            self.assertGreater(timeout, 0)
            if argv[-1] == "--version":
                return CommandResult(0, "codex-cli 0.145.0\n", "")
            if argv[-2:] == ["exec", "--help"]:
                return CommandResult(0, "--json\n--profile\n", "")
            if argv[-2:] == ["app-server", "--help"]:
                return CommandResult(1, "", "not installed")
            raise AssertionError(argv)

        result = probe_codex(["codex"], runner=runner)
        self.assertTrue(result["native_exec_ready"])
        self.assertFalse(result["app_server_ready"])
        self.assertEqual(result["subagent_mode"], "attempt-local")

    def test_probe_fails_closed_for_old_or_ambiguous_version(self) -> None:
        for version in ("codex-cli 0.144.1", "codex unknown"):
            def runner(argv: list[str], timeout: float, value: str = version) -> CommandResult:
                if argv[-1] == "--version":
                    return CommandResult(0, value, "")
                return CommandResult(0, "--json\n--profile\n", "")

            result = probe_codex(["codex"], runner=runner)
            self.assertFalse(result["native_exec_ready"])
            self.assertEqual(result["subagent_mode"], "disabled")

    def test_cli_exposes_machine_readable_status_command(self) -> None:
        args = build_parser().parse_args(
            ["codex-native-status", "--require-app-server"]
        )
        self.assertEqual(args.minimum_version, "0.145.0")
        self.assertTrue(args.require_app_server)

    def test_custom_runner_is_never_probed_for_native_subagents(self) -> None:
        actor = self.actor()
        actor["runner"]["executable"] = "fake-provider-runner"  # type: ignore[index]
        self.assertFalse(uses_default_codex_command(actor))

    def test_worker_semver_contract_matches_python(self) -> None:
        self.assertEqual(parse_version("codex-cli 0.145.0"), (0, 145, 0))
        script = (
            "const w=require('./container/worker/costmarshal-worker.js');"
            "process.stdout.write(JSON.stringify({"
            "ok:w.versionAtLeast('codex-cli 0.145.0','0.145.0'),"
            "old:w.versionAtLeast('0.144.1','0.145.0'),"
            "parsed:w.semanticVersion('codex-cli 0.145.0')}));"
        )
        completed = subprocess.run(
            ["node", "-e", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(
            result,
            {"ok": True, "old": False, "parsed": [0, 145, 0]},
        )


if __name__ == "__main__":
    unittest.main()
