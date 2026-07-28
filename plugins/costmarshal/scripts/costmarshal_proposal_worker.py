from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from costmarshal_v2.actor_runner import (  # noqa: E402
    ProposalApiError,
    _run_longcat_proposal,
)
from costmarshal_v2.security import redact_secret_values  # noqa: E402
from costmarshal_v2.state import atomic_write_text  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Trusted report-only provider adapter for CostMarshal"
    )
    parser.add_argument("--profile-file", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-output-tokens", required=True, type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    api_key = os.environ.get("LONGCAT_API_KEY", "")
    try:
        profile_payload = args.profile_file.read_bytes()
        if len(profile_payload) > 256 * 1024:
            raise ProposalApiError("proposal profile exceeds 256 KiB")
        prompt_text = sys.stdin.read()
        if not prompt_text.strip():
            raise ProposalApiError("proposal prompt is empty")
        content, usage = _run_longcat_proposal(
            actor={"provider": args.provider, "model": args.model},
            profile_payload=profile_payload,
            api_key=api_key,
            prompt_text=prompt_text,
            context_text="",
            max_output_tokens=args.max_output_tokens,
        )
        safe_content = redact_secret_values(content, (api_key,))
        report = (
            "# Completion Report\n\n"
            "Status: done\n\n"
            "## Report-only Provider Proposal\n\n"
            f"{safe_content.rstrip()}\n\n"
            "## Execution Boundary\n\n"
            "- No provider tool calls or workspace writes were permitted.\n"
            "- This proposal requires Codex Worker or Leader review before application.\n"
        )
        atomic_write_text(args.report, report)
        print(
            json.dumps(
                {
                    "type": "proposal.completed",
                    "usage": usage,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except (OSError, ProposalApiError) as exc:
        message = redact_secret_values(str(exc), (api_key,))[:1024]
        usage = exc.usage if isinstance(exc, ProposalApiError) else None
        atomic_write_text(
            args.report,
            "# Completion Report\n\n"
            "Status: failed\n\n"
            "## Proposal Adapter Failure\n\n"
            f"{type(exc).__name__}: {message}\n",
        )
        print(
            json.dumps(
                {
                    "type": "proposal.failed",
                    **({"usage": usage} if usage is not None else {}),
                    "error": {
                        "kind": type(exc).__name__,
                        "message": message,
                    },
                },
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
