from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.actor_runner import (  # noqa: E402
    ProposalApiError,
    _proposal_context_text,
    _run_longcat_proposal,
)
from costmarshal_v2.profiles import provider_preset_profile_text  # noqa: E402


CLI = ROOT / "scripts" / "costmarshal.py"


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.payload[:limit]


class ProposalApiContractTest(unittest.TestCase):
    def test_context_is_bounded_to_allowlisted_utf8_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-proposal-context-") as temporary:
            workspace = Path(temporary)
            (workspace / "src").mkdir()
            (workspace / "src" / "app.py").write_text(
                "print('ok')\n",
                encoding="utf-8",
            )
            (workspace / "outside.txt").write_text(
                "must remain absent\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=workspace,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "CostMarshal Test"],
                cwd=workspace,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "baseline"],
                cwd=workspace,
                check=True,
            )
            (workspace / "src" / "app.py").write_text(
                "print('dirty host content')\n",
                encoding="utf-8",
            )
            (workspace / "src" / "cache.pyc").write_bytes(b"\xa7binary")
            text = _proposal_context_text(
                workspace,
                {
                    "allowed_context": ["src"],
                    "estimated_input_tokens": 1000,
                },
            )
            self.assertIn("===== src/app.py =====", text)
            self.assertIn("print('ok')", text)
            self.assertNotIn("dirty host content", text)
            self.assertNotIn("cache.pyc", text)
            self.assertNotIn("outside.txt", text)

            (workspace / ".env").write_text("SECRET=x\n", encoding="utf-8")
            with self.assertRaisesRegex(ProposalApiError, "sensitive"):
                _proposal_context_text(
                    workspace,
                    {
                        "allowed_context": [".env"],
                        "estimated_input_tokens": 1000,
                    },
                )

    def test_longcat_chat_adapter_prefers_authoritative_chat_usage(self) -> None:
        observed: dict[str, object] = {}

        def fake_urlopen(request: object, timeout: int) -> _Response:
            observed["url"] = getattr(request, "full_url")
            observed["timeout"] = timeout
            observed["body"] = json.loads(getattr(request, "data"))
            return _Response(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "Status: done\n\nResult: proposal",
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 679,
                        "completion_tokens": 474,
                        "input_tokens": 0,
                        "output_tokens": 0,
                    },
                }
            )

        with patch(
            "costmarshal_v2.actor_runner.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            content, usage = _run_longcat_proposal(
                actor={"provider": "longcat", "model": "LongCat-2.0"},
                profile_payload=provider_preset_profile_text(
                    "longcat-2.0"
                ).encode("utf-8"),
                api_key="test-secret",
                prompt_text="bounded prompt",
                context_text="\ncontext",
                max_output_tokens=2000,
            )

        self.assertEqual(content, "Status: done\n\nResult: proposal")
        self.assertEqual(
            usage,
            {
                "input_tokens": 679,
                "cached_input_tokens": 0,
                "output_tokens": 474,
            },
        )
        self.assertEqual(
            observed["url"],
            "https://api.longcat.chat/openai/v1/chat/completions",
        )
        body = observed["body"]
        assert isinstance(body, dict)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["temperature"], 0)

    def test_truncated_proposal_preserves_billable_usage(self) -> None:
        def fake_urlopen(_request: object, timeout: int) -> _Response:
            self.assertEqual(timeout, 300)
            return _Response(
                {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {
                                "role": "assistant",
                                "content": "partial review",
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 2500,
                    },
                }
            )

        with patch(
            "costmarshal_v2.actor_runner.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            with self.assertRaisesRegex(
                ProposalApiError,
                "did not finish",
            ) as raised:
                _run_longcat_proposal(
                    actor={"provider": "longcat", "model": "LongCat-2.0"},
                    profile_payload=provider_preset_profile_text(
                        "longcat-2.0"
                    ).encode("utf-8"),
                    api_key="test-secret",
                    prompt_text="bounded prompt",
                    context_text="",
                    max_output_tokens=2500,
                )
        self.assertEqual(
            raised.exception.usage,
            {
                "input_tokens": 100,
                "cached_input_tokens": 0,
                "output_tokens": 2500,
            },
        )

    def test_proposal_mode_is_report_only_and_requires_explicit_host_opt_in(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-proposal-cli-") as temporary:
            temp = Path(temporary)
            workspace = temp / "workspace"
            workspace.mkdir()
            (workspace / "brief.md").write_text("Review this.\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=workspace,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "CostMarshal Test"],
                cwd=workspace,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "baseline"],
                cwd=workspace,
                check=True,
            )
            env = dict(os.environ)
            env["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
            env["CODEX_HOME"] = str(temp / "codex-home")

            configured = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "configure-profiles",
                    "--codex-home",
                    env["CODEX_HOME"],
                ],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(configured.stdout)["status"], "ok")
            initialized = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "init",
                    "--objective",
                    "proposal contract",
                    "--workspace",
                    str(workspace),
                    "--backend",
                    "local",
                    "--governance",
                    "off",
                    "--allow-unsafe-native-workers",
                ],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            project = json.loads(initialized.stdout)["project"]
            created = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "new-task",
                    "--project",
                    project,
                    "--title",
                    "proposal",
                    "--purpose",
                    "return a bounded report",
                    "--task-type",
                    "analysis",
                    "--provider",
                    "longcat",
                    "--tier",
                    "low",
                    "--execution-mode",
                    "proposal-api",
                    "--estimated-input-tokens",
                    "1000",
                    "--estimated-output-tokens",
                    "500",
                    "--allowed-context",
                    "brief.md",
                ],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(created.stdout)["task_id"], "V2-0001")

            rejected = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "dispatch",
                    "--project",
                    project,
                    "--task",
                    "V2-0001",
                ],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn(
                "explicit host report-adapter opt-in",
                rejected.stdout + rejected.stderr,
            )
            dispatched = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "dispatch",
                    "--project",
                    project,
                    "--task",
                    "V2-0001",
                    "--unsafe-native",
                ],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(
                json.loads(dispatched.stdout)["actor_id"],
                "agent-v2-0001",
            )

    def test_worker_reads_stdin_as_utf8_with_bom_and_non_gbk_bytes(self) -> None:
        """The proposal worker must not crash on UTF-8 BOM or non-GBK input.

        The scheduler writes the prompt as UTF-8 over the pipe.  Reading the
        text stream with the console codepage (for example cp936/GBK) turns
        BOM/non-GBK bytes into lone surrogates, and re-encoding them crashes
        with UnicodeEncodeError before the provider call.  The worker must
        decode stdin bytes explicitly and only fail with a structured
        proposal.failed payload.
        """

        with tempfile.TemporaryDirectory(prefix="costmarshal-proposal-utf8-") as temporary:
            root = Path(temporary)
            profile = root / "longcat.config.toml"
            profile.write_text(
                provider_preset_profile_text("longcat-2.0"),
                encoding="utf-8",
            )
            report = root / "report.md"
            prompt = "\ufeff# 实测 UTF-8 BOM 与中文 prompt\n\nProduce a short proposal.\n"
            env = os.environ.copy()
            env["LONGCAT_API_KEY"] = "invalid-test-key"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "costmarshal_proposal_worker.py"),
                    "--profile-file",
                    str(profile),
                    "--report",
                    str(report),
                    "--provider",
                    "longcat",
                    "--model",
                    "LongCat-2.0",
                    "--max-output-tokens",
                    "64",
                ],
                input=prompt.encode("utf-8"),
                env=env,
                cwd=ROOT,
                text=False,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 1)
            stdout_text = completed.stdout.decode("utf-8", errors="replace")
            stderr_text = completed.stderr.decode("utf-8", errors="replace")
            self.assertIn('"type": "proposal.failed"', stdout_text)
            self.assertNotIn("UnicodeEncodeError", stdout_text + stderr_text)
            self.assertTrue(report.is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
