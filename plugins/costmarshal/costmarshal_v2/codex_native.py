"""Codex-native compatibility and attempt-local subagent contracts.

CostMarshal remains the cross-provider control plane.  A Codex process may
delegate bounded work to native child agents only inside the already-admitted
provider attempt, where model, credential, sandbox, budget, and lease are
inherited.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence


CODEX_NATIVE_SCHEMA = "costmarshal-codex-native-v1"
MINIMUM_CODEX_VERSION = "0.145.0"
DEFAULT_MAX_SUBAGENTS = 3
_VERSION_PATTERN = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str], float], CommandResult]


def subprocess_command_runner(argv: Sequence[str], timeout: float) -> CommandResult:
    completed = subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def parse_version(value: str) -> tuple[int, int, int] | None:
    match = _VERSION_PATTERN.search(value)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def default_policy(*, enabled: bool = True) -> dict[str, Any]:
    return {
        "schema_version": CODEX_NATIVE_SCHEMA,
        "enabled": enabled,
        "mode": "attempt-local",
        "minimum_cli_version": MINIMUM_CODEX_VERSION,
        "max_subagents": DEFAULT_MAX_SUBAGENTS,
        "max_depth": 1,
        "authority": "costmarshal",
        "cross_provider_children": False,
    }


def normalized_policy(actor: dict[str, Any]) -> dict[str, Any]:
    runner = actor.get("runner")
    raw = runner.get("codex_native") if isinstance(runner, dict) else None
    if raw is None:
        return default_policy(enabled=False)
    if not isinstance(raw, dict):
        raise ValueError("runner.codex_native must be an object")
    policy = {**default_policy(), **raw}
    if policy.get("schema_version") != CODEX_NATIVE_SCHEMA:
        raise ValueError("runner.codex_native schema is unsupported")
    if policy.get("enabled") not in {True, False}:
        raise ValueError("runner.codex_native.enabled must be boolean")
    if policy.get("mode") != "attempt-local":
        raise ValueError("Codex-native mode must be attempt-local")
    if policy.get("authority") != "costmarshal":
        raise ValueError("Codex-native children cannot own routing or acceptance")
    if policy.get("cross_provider_children") is not False:
        raise ValueError("Codex-native children cannot switch provider credentials")
    maximum = policy.get("max_subagents")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 8:
        raise ValueError("Codex-native max_subagents must be between 1 and 8")
    if policy.get("max_depth") != 1:
        raise ValueError("Codex-native delegation depth must be exactly 1")
    minimum = str(policy.get("minimum_cli_version") or "")
    if parse_version(minimum) is None:
        raise ValueError("Codex-native minimum_cli_version is invalid")
    policy["minimum_cli_version"] = minimum
    return policy


def codex_native_overrides(actor: dict[str, Any]) -> list[str]:
    policy = normalized_policy(actor)
    if not policy["enabled"] or actor.get("role") != "agent":
        return []
    # Each native thread counts inside the same provider attempt.  The parent
    # occupies one slot, hence the +1.
    maximum_threads = int(policy["max_subagents"]) + 1
    return [
        "-c",
        "features.multi_agent=true",
        "-c",
        f"agents.max_concurrent_threads_per_session={maximum_threads}",
    ]


def uses_default_codex_command(actor: dict[str, Any]) -> bool:
    runner = actor.get("runner")
    if isinstance(runner, dict) and any(
        runner.get(key) for key in ("command_prefix", "executable")
    ):
        return False
    return not bool(os.environ.get("COSTMARSHAL_CODEX_COMMAND_JSON"))


def prompt_contract(actor: dict[str, Any]) -> list[str]:
    policy = normalized_policy(actor)
    if not policy["enabled"] or actor.get("role") != "agent":
        return []
    return [
        "## Codex-native Attempt Team",
        (
            f"- You may delegate independent inspection, implementation, or review to at most "
            f"{policy['max_subagents']} native Codex child agents."
        ),
        "- Child agents are optional; use them only when parallel work reduces risk or latency.",
        "- Every child stays inside this attempt's model, provider credential, sandbox, visible context, write scope, token reservation, and deadline.",
        "- Child agents must not route to another provider, request a new credential, accept the task, mutate CostMarshal control state, or spawn further children.",
        "- The parent remains responsible for reconciling child results and returning the single final CostMarshal report.",
    ]


def _run_probe(
    runner: CommandRunner,
    argv: Sequence[str],
    *,
    timeout: float,
) -> CommandResult:
    try:
        return runner(argv, timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return CommandResult(127, "", f"{type(exc).__name__}: {exc}")


def probe_codex(
    command: Sequence[str] = ("codex",),
    *,
    minimum_version: str = MINIMUM_CODEX_VERSION,
    timeout: float = 5.0,
    runner: CommandRunner = subprocess_command_runner,
) -> dict[str, Any]:
    if not command:
        raise ValueError("Codex command must not be empty")
    minimum = parse_version(minimum_version)
    if minimum is None:
        raise ValueError("minimum Codex version is invalid")
    version_result = _run_probe(runner, [*command, "--version"], timeout=timeout)
    observed = parse_version(f"{version_result.stdout}\n{version_result.stderr}")
    exec_help = _run_probe(runner, [*command, "exec", "--help"], timeout=timeout)
    app_help = _run_probe(runner, [*command, "app-server", "--help"], timeout=timeout)
    exec_text = f"{exec_help.stdout}\n{exec_help.stderr}"
    checks = {
        "version_detected": observed is not None,
        "minimum_version": observed is not None and observed >= minimum,
        "exec_json": exec_help.returncode == 0 and "--json" in exec_text,
        "exec_profile": exec_help.returncode == 0 and "--profile" in exec_text,
        "app_server": app_help.returncode == 0,
    }
    native_exec_ready = all(checks[key] for key in ("version_detected", "minimum_version", "exec_json", "exec_profile"))
    return {
        "schema_version": CODEX_NATIVE_SCHEMA,
        "command": list(command),
        "observed_version": ".".join(str(part) for part in observed) if observed else None,
        "minimum_version": minimum_version,
        "native_exec_ready": native_exec_ready,
        "app_server_ready": checks["app_server"],
        "subagent_mode": "attempt-local" if native_exec_ready else "disabled",
        "cross_provider_children": False,
        "authority": "costmarshal",
        "checks": checks,
        "errors": [
            text
            for text in (
                version_result.stderr.strip() if version_result.returncode else "",
                exec_help.stderr.strip() if exec_help.returncode else "",
                app_help.stderr.strip() if app_help.returncode else "",
            )
            if text
        ],
    }


def command_codex_native_status(args: Any) -> None:
    command = [str(getattr(args, "codex_command", None) or "codex")]
    if os.name == "nt":
        # Import lazily to avoid an actor_runner -> codex_native import cycle.
        # The shared resolver turns the reviewed npm .cmd shim into a native
        # node argv and never invokes cmd.exe.
        from .actor_runner import resolve_codex_command

        command = resolve_codex_command(
            {}
            if command[0] == "codex"
            else {"runner": {"executable": command[0]}}
        )
    payload = probe_codex(
        command,
        minimum_version=str(
            getattr(args, "minimum_version", None) or MINIMUM_CODEX_VERSION
        ),
        timeout=float(getattr(args, "timeout", None) or 5.0),
    )
    if getattr(args, "require_app_server", False):
        payload["ready"] = bool(
            payload["native_exec_ready"] and payload["app_server_ready"]
        )
    else:
        payload["ready"] = bool(payload["native_exec_ready"])
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def version_at_least(value: str, minimum: str = MINIMUM_CODEX_VERSION) -> bool:
    observed = parse_version(value)
    required = parse_version(minimum)
    return observed is not None and required is not None and observed >= required


__all__ = [
    "CODEX_NATIVE_SCHEMA",
    "DEFAULT_MAX_SUBAGENTS",
    "MINIMUM_CODEX_VERSION",
    "CommandResult",
    "codex_native_overrides",
    "command_codex_native_status",
    "default_policy",
    "normalized_policy",
    "parse_version",
    "probe_codex",
    "prompt_contract",
    "version_at_least",
    "uses_default_codex_command",
]
