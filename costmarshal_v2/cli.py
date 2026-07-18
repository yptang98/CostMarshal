from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import __version__
from .control_store import (
    control_store_status,
    migrate_legacy_store,
    preview_legacy_migration,
    reconcile_project_views,
)
from .locking import (
    ProjectLockTimeout,
    project_write_lock,
    scheduler_daemon_lock,
    scheduler_instance_lock,
)
from .paths import default_root, resolve_project
from .profiles import command_configure_profiles, command_configure_provider
from .scheduler import (
    LEADER_WORK_TYPES,
    RISKS,
    command_apply_changes,
    command_collect,
    command_dispatch,
    command_escalate,
    command_heartbeat,
    command_init,
    command_new_task,
    command_dashboard,
    command_budget_status,
    command_governance_status,
    command_governance_rebind,
    command_providers,
    command_preview_changes,
    command_record_leader_work,
    command_record_result,
    command_record_usage,
    command_recover,
    command_relay,
    command_route,
    command_run_scheduler,
    command_send,
    command_start_leader,
    command_stop_actor,
    command_status,
    command_validate,
)


def _add_command_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--command-id",
        help="Stable idempotency key for safe retry after a crash; scheduler messages use their message id",
    )


def command_migrate_state(args: argparse.Namespace) -> None:
    layout = resolve_project(args.root, args.project)
    if not args.apply:
        payload = preview_legacy_migration(layout)
    else:
        try:
            with scheduler_daemon_lock(layout, timeout_seconds=0.25):
                with scheduler_instance_lock(layout, timeout_seconds=0.25):
                    with project_write_lock(layout):
                        payload = migrate_legacy_store(layout)
        except ProjectLockTimeout as exc:
            raise SystemExit(f"state migration requires exclusive scheduler/project ownership: {exc}") from exc
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def command_state_store_status(args: argparse.Namespace) -> None:
    layout = resolve_project(args.root, args.project)
    if args.repair_views:
        try:
            with project_write_lock(layout):
                reconcile_project_views(layout)
        except ProjectLockTimeout as exc:
            raise SystemExit(str(exc)) from exc
    payload = control_store_status(layout)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CostMarshal v3 scheduler")
    parser.add_argument("--root", type=Path, default=default_root(), help="CostMarshal runtime root (v2 path compatible)")
    parser.add_argument("--version", action="version", version=f"CostMarshal {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    configure_profiles = sub.add_parser("configure-profiles", help="Create the user-level LongCat Codex profile without storing an API key")
    configure_profiles.add_argument("--codex-home")
    configure_profiles.add_argument("--longcat-profile", default="longcat")
    configure_profiles.add_argument("--force", action="store_true")
    configure_profiles.add_argument("--dry-run", action="store_true")
    configure_profiles.set_defaults(func=command_configure_profiles)

    configure_provider = sub.add_parser("configure-provider", help="Create a user-level Codex profile for a custom API without storing its key")
    configure_provider.add_argument("--codex-home")
    configure_provider.add_argument("--profile", required=True)
    configure_provider.add_argument("--provider-id", required=True)
    configure_provider.add_argument("--display-name")
    configure_provider.add_argument("--base-url", required=True)
    configure_provider.add_argument("--model", required=True)
    configure_provider.add_argument("--env-key", required=True)
    configure_provider.add_argument("--wire-api")
    configure_provider.add_argument("--reasoning-effort", choices=["minimal", "low", "medium", "high", "xhigh"])
    configure_provider.add_argument("--force", action="store_true")
    configure_provider.add_argument("--dry-run", action="store_true")
    configure_provider.set_defaults(func=command_configure_provider)

    init = sub.add_parser("init", help="Create a v3 project without touching compatible legacy state")
    init.add_argument("--name", default="")
    init.add_argument("--objective", required=True)
    init.add_argument("--source-project", help="Optional existing project to reference read-only")
    init.add_argument("--workspace", help="Writable workspace used by Codex and LongCat actors; defaults to the current directory")
    init.add_argument("--secrets-file", help="Optional local env file loaded only into actor subprocesses")
    init.add_argument("--provider-catalog", type=Path, help="Reviewed JSON provider catalog; defaults to LongCat/DeepSeek/Codex low/medium/high tiers")
    init.add_argument("--project-budget-cny", help="Optional hard project budget (up to 9 decimal places); requires reviewed prices for routed providers")
    init.add_argument(
        "--default-min-success-probability",
        type=float,
        help="Optional project success-probability SLA (0..1), frozen onto each new auto-routed task unless overridden",
    )
    init.add_argument(
        "--routing-objective",
        choices=["completion-first", "cost-only"],
        default="completion-first",
        help="Default auto-route objective; completion-first requires a strongest-tier terminal fallback",
    )
    init.add_argument("--governance", choices=["off", "auto", "required"], default="auto", help="ArchMarshal governance gate; checks are strictly read-only")
    init.add_argument(
        "--archmarshal-launcher",
        type=Path,
        help="Exact reviewed canonical run_archmarshal.py path for read-only governance checks",
    )
    init.add_argument(
        "--archmarshal-wrapper",
        type=Path,
        help=argparse.SUPPRESS,
    )
    init.add_argument(
        "--no-auto-escalate",
        dest="auto_escalate",
        action="store_false",
        default=False,
        help=(
            "Deprecated compatibility flag; workers always wait for an explicit "
            "leader-rejected result and escalation command"
        ),
    )
    init.add_argument("--session-name", help="Backend session name; defaults to cmv2-<project>")
    init.add_argument("--backend", choices=["auto", "tmux", "local"], default="auto", help="Actor runtime backend; auto chooses a platform-appropriate backend")
    init.add_argument("--backend-command", help="Backend executable, for example a tmux binary when --backend tmux")
    init.add_argument("--leader-model", default="inherit")
    init.add_argument("--leader-profile")
    init.add_argument("--leader-command", help="Legacy custom manager command; default uses the structured codex exec runner")
    init.add_argument("--allow-unsafe-custom-worker-commands", action="store_true", help="Privileged compatibility escape hatch; bypasses worker sandbox and secret isolation")
    init.add_argument("--worker-isolation", choices=["required"], default="required", help="Require attested Linux OCI isolation for task workers")
    init.add_argument("--container-engine", choices=["auto", "docker", "podman"], default="auto")
    init.add_argument("--worker-image", help="Digest-pinned worker image, name@sha256:<64 hex>")
    init.add_argument("--worker-network", choices=["none", "provider-proxy"], default="provider-proxy")
    init.add_argument("--worker-network-name", default="costmarshal-provider-proxy")
    init.add_argument("--allow-unsafe-native-workers", action="store_true", help="Project-level half of the explicit development-only native worker escape hatch")
    init.add_argument("--tmux-command", dest="backend_command", help=argparse.SUPPRESS)
    init.set_defaults(func=command_init)

    start_leader = sub.add_parser("start-leader", help="Run one on-demand Codex manager turn (legacy command name)")
    start_leader.add_argument("--project", required=True)
    start_leader.add_argument("--model")
    start_leader.add_argument("--profile")
    start_leader.add_argument("--command")
    start_leader.add_argument("--dry-run", action="store_true")
    start_leader.set_defaults(func=command_start_leader)

    run_manager = sub.add_parser("run-manager", help="Run one on-demand Codex manager turn")
    run_manager.add_argument("--project", required=True)
    run_manager.add_argument("--model")
    run_manager.add_argument("--profile")
    run_manager.add_argument("--command")
    run_manager.add_argument("--dry-run", action="store_true")
    run_manager.set_defaults(func=command_start_leader)

    new_task = sub.add_parser("new-task", help="Create a v2 bounded task")
    new_task.add_argument("--project", required=True)
    new_task.add_argument("--id")
    new_task.add_argument("--title", required=True)
    new_task.add_argument("--purpose", required=True)
    new_task.add_argument("--task-type", default="analysis")
    new_task.add_argument("--risk", choices=["low", "medium", "high"], default="low")
    new_task.add_argument("--difficulty", choices=["simple", "normal", "hard"], default="normal")
    new_task.add_argument("--provider", default="auto", help="Provider id from the project catalog, or auto")
    new_task.add_argument("--tier", choices=["auto", "low", "medium", "high"], default="auto")
    new_task.add_argument("--profile")
    new_task.add_argument("--agent", default="auto")
    new_task.add_argument("--model", default="inherit")
    new_task.add_argument(
        "--estimated-input-tokens",
        type=int,
        default=0,
        help="Estimated ordinary (non-cached) input tokens",
    )
    new_task.add_argument(
        "--estimated-cached-input-tokens",
        type=int,
        default=0,
        help=(
            "Estimated cached input tokens; without a frozen cache-origin identity, "
            "v3 forecasts them conservatively at the ordinary-input rate"
        ),
    )
    new_task.add_argument("--estimated-output-tokens", type=int, default=0)
    new_task.add_argument("--max-cost-cny", help="Task budget in CNY, with up to 9 decimal places")
    new_task.add_argument("--require-capability", action="append", dest="required_capabilities")
    new_task.add_argument("--min-success-probability", type=float)
    new_task.add_argument(
        "--routing-objective",
        choices=["completion-first", "cost-only"],
        help="Override the project's auto-route objective for this task",
    )
    new_task.add_argument("--acceptance", action="append")
    new_task.add_argument("--allowed-context", action="append")
    new_task.add_argument("--allowed-path", action="append")
    new_task.add_argument("--claim-path", action="append", help="File or directory path this task claims for writing")
    new_task.add_argument("--allow-lock-conflict", action="store_true", help="Allow overlapping claimed paths after explicit leader review")
    _add_command_id(new_task)
    new_task.set_defaults(func=command_new_task)

    route = sub.add_parser("route", help="Explain or simulate a safe cost-performance route without changing state")
    route.add_argument("--project", required=True)
    route.add_argument("--task-type", default="analysis")
    route.add_argument("--risk", choices=["low", "medium", "high"], default="low")
    route.add_argument("--difficulty", choices=["simple", "normal", "hard"], default="normal")
    route.add_argument("--provider", default="auto")
    route.add_argument("--tier", choices=["auto", "low", "medium", "high"], default="auto")
    route.add_argument(
        "--estimated-input-tokens",
        type=int,
        default=0,
        help="Estimated ordinary (non-cached) input tokens",
    )
    route.add_argument(
        "--estimated-cached-input-tokens",
        type=int,
        default=0,
        help=(
            "Estimated cached input tokens; without a frozen cache-origin identity, "
            "v3 forecasts them conservatively at the ordinary-input rate"
        ),
    )
    route.add_argument("--estimated-output-tokens", type=int, default=0)
    route.add_argument("--require-capability", action="append", dest="required_capabilities")
    route.add_argument("--min-success-probability", type=float)
    route.add_argument(
        "--routing-objective",
        choices=["completion-first", "cost-only"],
        help="Override the project's auto-route objective for this read-only explanation",
    )
    route.set_defaults(func=command_route)

    providers = sub.add_parser("providers", help="List and validate the project provider catalog")
    providers.add_argument("--project", required=True)
    providers.set_defaults(func=command_providers)

    budget = sub.add_parser("budget", help="Show project budget commitment and per-attempt reservations")
    budget.add_argument("--project", required=True)
    budget.set_defaults(func=command_budget_status)

    governance_status = sub.add_parser("governance-status", help="Validate the stored ArchMarshal governance binding without mutation")
    governance_status.add_argument("--project", required=True)
    governance_status.set_defaults(func=command_governance_status)

    governance_rebind = sub.add_parser(
        "governance-rebind",
        help="Preview or explicitly refresh only CostMarshal's read-only ArchMarshal binding",
    )
    governance_rebind.add_argument("--project", required=True)
    governance_rebind.add_argument("--archmarshal-launcher", type=Path)
    governance_rebind.add_argument("--archmarshal-wrapper", type=Path, help=argparse.SUPPRESS)
    governance_rebind.add_argument("--apply", action="store_true")
    _add_command_id(governance_rebind)
    governance_rebind.set_defaults(func=command_governance_rebind)

    dispatch = sub.add_parser("dispatch", help="Assign a task to an agent actor and optionally start it")
    dispatch.add_argument("--project", required=True)
    dispatch.add_argument("--task", required=True)
    dispatch.add_argument("--actor-id")
    dispatch.add_argument("--agent")
    dispatch.add_argument("--model")
    dispatch.add_argument("--provider", help="Provider id from the project catalog, or auto")
    dispatch.add_argument("--tier", choices=["auto", "low", "medium", "high"])
    dispatch.add_argument("--profile")
    dispatch.add_argument("--command")
    dispatch.add_argument("--start", action="store_true")
    dispatch.add_argument("--dry-run", action="store_true")
    dispatch.add_argument("--force", action="store_true")
    dispatch.add_argument("--unsafe-native", action="store_true", help="Dispatch-level half of the development-only native worker escape hatch")
    _add_command_id(dispatch)
    dispatch.set_defaults(func=command_dispatch)

    escalate = sub.add_parser("escalate", help="Continue a leader-rejected task to the exact next provider in its admitted non-decreasing chain")
    escalate.add_argument("--project", required=True)
    escalate.add_argument("--task", required=True)
    escalate.add_argument("--reason", required=True)
    escalate.add_argument("--actor-id")
    escalate.add_argument("--from-actor", help=argparse.SUPPRESS)
    escalate.add_argument("--attempt", help=argparse.SUPPRESS)
    escalate.add_argument("--profile")
    escalate.add_argument("--model")
    escalate.add_argument("--provider", help="Explicit stronger provider id")
    escalate.add_argument("--to-tier", choices=["medium", "high"], help="Explicit stronger target tier")
    escalate.add_argument("--start", action="store_true")
    escalate.add_argument("--dry-run", action="store_true")
    escalate.add_argument("--force", action="store_true")
    escalate.add_argument(
        "--replan",
        action="store_true",
        help="Explicitly archive the active remaining plan and atomically re-admit the selected next step",
    )
    escalate.add_argument("--unsafe-native", action="store_true", help="Explicitly continue an unsafe-native attempt during manual escalation")
    _add_command_id(escalate)
    escalate.set_defaults(func=command_escalate)

    send = sub.add_parser("send", help="Relay a mailbox message")
    send.add_argument("--project", required=True)
    send.add_argument("--to", required=True)
    send.add_argument("--sender", default="scheduler")
    send.add_argument("--subject", default="Scheduler message")
    send.add_argument("--message", required=True)
    send.add_argument("--task")
    send.add_argument("--runtime-send", action="store_true", help="Also inject the text into the actor runtime when that backend supports it")
    send.add_argument("--tmux-send", dest="runtime_send", action="store_true", help=argparse.SUPPRESS)
    _add_command_id(send)
    send.set_defaults(func=command_send)

    relay = sub.add_parser("relay", help="Deliver actor-authored outbox messages using a durable cursor")
    relay.add_argument("--project", required=True)
    relay.add_argument("--actor", required=True)
    relay.add_argument("--limit", type=int)
    relay.add_argument("--dry-run", action="store_true")
    relay.set_defaults(func=command_relay)

    run_scheduler = sub.add_parser("run-scheduler", help="Run the small scheduler loop that relays actor outboxes and executes actor-authored commands")
    run_scheduler.add_argument("--project", required=True)
    run_scheduler.add_argument("--interval", type=float, default=2.0)
    run_scheduler.add_argument("--once", action="store_true", help="Run one cycle and exit")
    run_scheduler.add_argument("--max-cycles", type=int, default=0, help="Run a bounded number of cycles; 0 means forever unless --once is set")
    run_scheduler.add_argument("--relay-limit", type=int)
    run_scheduler.add_argument("--command-limit", type=int)
    run_scheduler.add_argument("--dry-run", action="store_true")
    run_scheduler.set_defaults(func=command_run_scheduler)

    heartbeat = sub.add_parser("heartbeat", help="Record an actor heartbeat")
    heartbeat.add_argument("--project", required=True)
    heartbeat.add_argument("--actor", required=True)
    heartbeat.add_argument("--status", default="running")
    heartbeat.add_argument("--note")
    _add_command_id(heartbeat)
    heartbeat.set_defaults(func=command_heartbeat)

    stop_actor = sub.add_parser("stop-actor", help="Mark an actor stopped and optionally stop its runtime process")
    stop_actor.add_argument("--project", required=True)
    stop_actor.add_argument("--actor", required=True)
    stop_actor.add_argument("--reason")
    stop_actor.add_argument("--stop-runtime", action="store_true")
    stop_actor.add_argument("--kill-window", dest="stop_runtime", action="store_true", help=argparse.SUPPRESS)
    stop_actor.add_argument("--dry-run", action="store_true")
    _add_command_id(stop_actor)
    stop_actor.set_defaults(func=command_stop_actor)

    collect = sub.add_parser("collect", help="Relay task report availability to the leader")
    collect.add_argument("--project", required=True)
    collect.add_argument("--task", required=True)
    collect.add_argument("--actor")
    collect.add_argument("--attempt")
    collect.add_argument("--state", default="waiting_leader")
    collect.add_argument("--report")
    collect.add_argument("--summary", help="Optional caller-provided compact summary; scheduler does not infer one")
    _add_command_id(collect)
    collect.set_defaults(func=command_collect)

    preview_changes = sub.add_parser(
        "preview-changes",
        help="Build a source-isolated Git patch/tree preview for a sealed worker output",
    )
    preview_changes.add_argument("--project", required=True)
    preview_changes.add_argument("--task", required=True)
    preview_changes.add_argument("--attempt")
    _add_command_id(preview_changes)
    preview_changes.set_defaults(func=command_preview_changes)

    apply_changes = sub.add_parser(
        "apply-changes",
        help="Preview or explicitly stage a leader-accepted sealed change set",
    )
    apply_changes.add_argument("--project", required=True)
    apply_changes.add_argument("--task", required=True)
    apply_changes.add_argument("--attempt")
    apply_changes.add_argument(
        "--apply",
        action="store_true",
        help="Stage the exact reviewed patch after HEAD/clean-worktree compare-and-swap",
    )
    apply_changes.add_argument(
        "--preview-sha",
        help="Exact preview_sha256 printed by the preceding read-only invocation",
    )
    _add_command_id(apply_changes)
    apply_changes.set_defaults(func=command_apply_changes)

    result = sub.add_parser("record-result", help="Record leader acceptance/rejection for a worker attempt")
    result.add_argument("--project", required=True)
    result.add_argument("--task", required=True)
    result.add_argument("--status", choices=["done", "failed", "escalate"], required=True)
    result.add_argument("--quality-score", type=int, choices=[1, 2, 3, 4, 5], required=True)
    result.add_argument("--accepted-by-leader", action="store_true")
    result.add_argument("--agent")
    result.add_argument("--actor")
    result.add_argument("--attempt")
    result.add_argument("--model")
    result.add_argument(
        "--input-tokens",
        type=int,
        default=0,
        help="Ordinary input tokens, excluding cached input tokens",
    )
    result.add_argument(
        "--cached-input-tokens",
        type=int,
        default=0,
        help="Cached input tokens, recorded separately from ordinary input",
    )
    result.add_argument("--output-tokens", type=int, default=0)
    result.add_argument("--estimated-cost-cny", help="Unverified caller-reported cost, up to 9 decimal places")
    result.add_argument("--summary")
    result.add_argument(
        "--handoff",
        help="Bound successor handoff for a rejected sealed required attempt",
    )
    result.add_argument("--note")
    _add_command_id(result)
    result.set_defaults(func=command_record_result)

    leader_work = sub.add_parser("record-leader-work", help="Audit direct leader implementation-like work")
    leader_work.add_argument("--project", required=True)
    leader_work.add_argument("--task")
    leader_work.add_argument("--work-type", choices=sorted(LEADER_WORK_TYPES), default="other")
    leader_work.add_argument("--risk", choices=sorted(RISKS), default="medium")
    leader_work.add_argument("--scope", required=True)
    leader_work.add_argument("--reason", required=True)
    leader_work.add_argument("--file", action="append")
    leader_work.add_argument("--minutes", type=int, default=0)
    leader_work.add_argument("--model", default="codex-leader")
    leader_work.add_argument(
        "--input-tokens",
        type=int,
        default=0,
        help="Ordinary input tokens, excluding cached input tokens",
    )
    leader_work.add_argument(
        "--cached-input-tokens",
        type=int,
        default=0,
        help="Cached input tokens, recorded separately from ordinary input",
    )
    leader_work.add_argument("--output-tokens", type=int, default=0)
    leader_work.add_argument("--estimated-cost-cny", help="Audited cost, up to 9 decimal places")
    leader_work.add_argument("--note")
    _add_command_id(leader_work)
    leader_work.set_defaults(func=command_record_leader_work)

    usage = sub.add_parser("record-usage", help="Record actor-reported token usage while work is in progress")
    usage.add_argument("--project", required=True)
    usage.add_argument("--actor", required=True)
    usage.add_argument("--task")
    usage.add_argument("--attempt")
    usage.add_argument("--model")
    usage.add_argument(
        "--input-tokens",
        type=int,
        default=None,
        help="Ordinary input tokens; supplying any token flag asserts omitted token dimensions are zero",
    )
    usage.add_argument(
        "--cached-input-tokens",
        type=int,
        default=None,
        help="Cached input tokens; supplying any token flag asserts omitted token dimensions are zero",
    )
    usage.add_argument(
        "--output-tokens",
        type=int,
        default=None,
        help="Output tokens; omit all three token flags when provider usage is unknown",
    )
    usage.add_argument("--estimated-cost-cny", help="Unverified caller-reported cost, up to 9 decimal places")
    usage.add_argument("--final", dest="final_usage", action="store_true", help="Mark this as terminal usage and settle the remaining reservation")
    usage.add_argument("--note")
    _add_command_id(usage)
    usage.set_defaults(func=command_record_usage)

    recover = sub.add_parser("recover", help="Audit v2 session, mailbox, and backend recoverability")
    recover.add_argument("--project", required=True)
    recover.add_argument("--plan-restarts", action="store_true")
    recover.add_argument("--restart-missing", action="store_true", help="Restart missing runtimes for actors that were marked running")
    _add_command_id(recover)
    recover.set_defaults(func=command_recover)

    dashboard = sub.add_parser("dashboard", help="Show a live process board for scheduler, leader, agents, mailboxes, and token totals")
    dashboard.add_argument("--project", required=True)
    dashboard.add_argument("--format", choices=["json", "md"], default="md")
    dashboard.add_argument("--watch", action="store_true")
    dashboard.add_argument("--interval", type=float, default=2.0)
    dashboard.set_defaults(func=command_dashboard)

    status = sub.add_parser("status", help="Show CostMarshal project status")
    status.add_argument("--project", required=True)
    status.add_argument("--format", choices=["json", "md"], default="md")
    status.set_defaults(func=command_status)

    validate = sub.add_parser("validate", help="Validate CostMarshal project structure")
    validate.add_argument("--project", required=True)
    validate.set_defaults(func=command_validate)

    migrate_state = sub.add_parser("migrate-state", help="Preview or explicitly enable the SQLite WAL control store")
    migrate_state.add_argument("--project", required=True)
    migrate_state.add_argument("--apply", action="store_true", help="Back up legacy views and atomically cut over to SQLite")
    migrate_state.set_defaults(func=command_migrate_state)

    store_status = sub.add_parser("state-store", help="Inspect SQLite integrity, schema, journal mode, and dirty views")
    store_status.add_argument("--project", required=True)
    store_status.add_argument("--repair-views", action="store_true", help="Rebuild committed dirty compatibility views")
    store_status.set_defaults(func=command_state_store_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0
