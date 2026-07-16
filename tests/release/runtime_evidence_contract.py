"""Shared contract for machine-derived runtime recovery evidence."""

from __future__ import annotations


RECEIPT_PREFIX = "COSTMARSHAL_RUNTIME_EVIDENCE="
RUNTIME_EVIDENCE_TESTS = (
    "tests/runtime_effect_store_test.py",
    "tests/runtime_effect_scheduler_test.py",
    "tests/actor_crash_recovery_test.py",
    "tests/runtime_recovery_reliability_test.py",
    "tests/oci_actor_runner_test.py",
)
REQUIRED_RUNTIME_CRASH_POINTS = (
    "effect.after_lease_commit_before_spawn",
    "effect.after_registration_before_finalize",
    "schema.after_effect_hash_backfill_row",
    "effect.after_spawn_before_observe",
    "effect.after_stop_before_observe",
    "effect.after_stop_observe_before_apply",
    "transaction.after_commit_before_materialize",
    "after_attempt_report_before_publish",
    "after_credential_before_oci_prepare",
    "after_oci_prepare_before_start",
    "after_external_create_before_durable_identity",
    "effect.after_dead_status_before_projection",
)
REQUIRED_RUNTIME_RECOVERY_SCENARIOS = (
    "corrupt_project_emergency_stop_only",
    "daemon_sleep_does_not_block_emergency_stop",
    "governance_drift_emergency_stop_replay",
    "materializer_revision_aba_serialized",
    "materializer_transient_sharing_retry_bounded",
    "runner_exit_before_provider_start",
    "no_effect_commit_view_reconciled_by_scheduler",
    "slow_stop_lease_heartbeat_single_execution",
    "oci_stop_after_rm_before_observe",
    "credential_after_create_before_oci_prepare",
    "oci_prepared_before_start",
    "deterministic_name_attach_after_hard_exit",
    "cleanup_unconfirmed_preserves_credential",
    "recovered_usage_unknown_preserves_budget_reservation",
)
