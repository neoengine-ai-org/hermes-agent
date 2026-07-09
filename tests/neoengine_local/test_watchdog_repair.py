from __future__ import annotations

import time
from pathlib import Path

from neoengine_local.runtime.watchdog_repair import (
    CANONICAL_NEOWEALTH_RETAINED_LANES,
    lane_registration_preflight_receipt,
    missing_required_lane_registrations,
    proof_is_passing,
    run_dynamic_runtime_repair,
    runtime_repair_commands,
    watchdog_runtime_status_after_repair,
)


def test_dynamic_repair_runs_proof_health_loop_retry_and_stops_on_pass(tmp_path):
    calls: list[list[str]] = []
    proofs = iter([
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 1 < required 4"]},
        {"proof_status": "PASS", "remaining_blockers": []},
    ])

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 1 < required 4"]},
        "neowealth",
        lambda: next(proofs),
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_attempted"] is True
    assert receipt["runtime_repair_status"] == "REPAIRED_PASS"
    assert watchdog_runtime_status_after_repair(receipt) == "RUNTIME_PASS"
    assert len(calls) == 2
    assert calls == runtime_repair_commands("neowealth", hermes_home=tmp_path)[:2]


def test_dynamic_repair_is_bounded_and_reports_failed_after_single_retry(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["still down"]},
        "neowealth",
        lambda: {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["still down"]},
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_ATTEMPTED_FAILED"
    assert watchdog_runtime_status_after_repair(receipt) == "RUNTIME_REPAIR_ATTEMPTED_FAILED"
    assert len(calls) == 3
    assert calls == runtime_repair_commands("neowealth", hermes_home=tmp_path)


def test_dynamic_repair_continues_after_restart_guard_nonzero_with_clean_kanban(tmp_path):
    calls: list[list[str]] = []
    proofs = iter([
        {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time() + 5,
            "remaining_blockers": [
                "live_codex_build_lanes 0 < required 2",
                "live_sonnet_build_lanes 0 < required 1",
            ],
            "lane_control_substrate_blockers": [],
            "lanes": [],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
                "resolver": "hermes_cli.kanban_db.kanban_db_path",
            },
        },
        {"proof_status": "PASS", "remaining_blockers": []},
    ])

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            stdout = "proof attempted"
            stderr = ""

            @property
            def returncode(self):
                return 2 if len(calls) == 1 else 0

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: next(proofs),
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIRED_PASS"
    assert len(calls) == 2
    assert calls == runtime_repair_commands("neoengine", hermes_home=tmp_path)[:2]
    assert receipt["actions"][0]["continued_after_nonzero"] is True
    assert receipt["actions"][0]["continue_reason"] == "recoverable_restart_guard_with_clean_kanban"


def test_dynamic_repair_continues_after_nonzero_with_clean_lane_attestation(tmp_path):
    calls: list[list[str]] = []
    proofs = iter([
        {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time() + 5,
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "lane_control_substrate_blockers": [],
            "lanes": [
                {
                    "lane_id": "NE-CODEX-01",
                    "expected_persistent": True,
                    "lane_control_consecutive_failures": 0,
                    "lane_control_heartbeat": {
                        "heartbeat": {
                            "last_error_at": None,
                            "last_error_message": None,
                        },
                    },
                },
            ],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
            },
        },
        {"proof_status": "PASS", "remaining_blockers": []},
    ])

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            stdout = "proof attempted"
            stderr = ""

            @property
            def returncode(self):
                return 2 if len(calls) == 1 else 0

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: next(proofs),
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIRED_PASS"
    assert len(calls) == 2
    assert receipt["actions"][0]["continued_after_nonzero"] is True


def test_dynamic_repair_fails_closed_after_nonzero_with_lane_malformed_blocker(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time(),
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "lane_control_substrate_blockers": [
                "KANBAN_DB_MALFORMED lane=NE-CODEX-01 consecutive_failures=19 error=database disk image is malformed",
            ],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
            },
        },
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 1
    assert "continued_after_nonzero" not in receipt["actions"][0]


def test_dynamic_repair_fails_closed_after_nonzero_with_hidden_substrate_failure(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time(),
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
                "per_lane": {"KANBAN_DB_MALFORMED": {"lane": "NE-CODEX-01"}},
            },
        },
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 1
    assert "continued_after_nonzero" not in receipt["actions"][0]


def test_dynamic_repair_fails_closed_after_nonzero_with_unclean_diagnostic_status(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time(),
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "lane_control_substrate_blockers": [],
            "lane_health": {"status": "PAGE_CHECKSUM_MISMATCH"},
            "lanes": [],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
            },
        },
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 1
    assert "continued_after_nonzero" not in receipt["actions"][0]


def test_dynamic_repair_fails_closed_after_nonzero_with_substrate_diagnostic_errors(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time(),
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "lane_control_substrate_blockers": [],
            "lanes": [],
            "substrate": {"errors": ["PAGE_CHECKSUM_MISMATCH"]},
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
            },
        },
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 1
    assert "continued_after_nonzero" not in receipt["actions"][0]


def test_dynamic_repair_fails_closed_after_nonzero_when_kanban_probe_blocks(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time(),
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "kanban_db_quick_check": {"status": "MALFORMED", "blocking": True},
        },
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 1
    assert "continued_after_nonzero" not in receipt["actions"][0]


def test_dynamic_repair_fails_closed_after_nonzero_when_clean_probe_lacks_db_path(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time(),
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "kanban_db_quick_check": {"status": "OK", "blocking": False},
        },
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 1
    assert "continued_after_nonzero" not in receipt["actions"][0]


def test_dynamic_repair_fails_closed_after_nonzero_with_unrecognized_blocker(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time(),
            "remaining_blockers": [
                "live_codex_build_lanes 0 < required 2",
                "PR_NOT_APPROVED",
            ],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
            },
        },
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 1
    assert "continued_after_nonzero" not in receipt["actions"][0]


def test_dynamic_repair_fails_closed_after_nonzero_without_lanes_attestation(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time(),
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "lane_control_substrate_blockers": [],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
            },
        },
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 1
    assert "continued_after_nonzero" not in receipt["actions"][0]


def test_dynamic_repair_fails_closed_after_nonzero_with_lane_control_failures(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time(),
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "lane_control_substrate_blockers": [],
            "lanes": [{"lane_id": "NE-CODEX-01", "lane_control_consecutive_failures": 1}],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
            },
        },
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 1
    assert "continued_after_nonzero" not in receipt["actions"][0]


def test_dynamic_repair_fails_closed_after_nonzero_with_corruption_counter(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time(),
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "lane_control_substrate_blockers": [],
            "lanes": [{"lane_id": "NE-CODEX-01", "page_corruption_count": 1}],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
            },
        },
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 1
    assert "continued_after_nonzero" not in receipt["actions"][0]


def test_dynamic_repair_fails_closed_after_nonzero_with_non_dict_lane(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time(),
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "lane_control_substrate_blockers": [],
            "lanes": ["NE-CODEX-01"],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
            },
        },
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 1
    assert "continued_after_nonzero" not in receipt["actions"][0]


def test_dynamic_repair_fails_closed_after_nonzero_with_bad_lane_heartbeat(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time(),
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "lane_control_substrate_blockers": [],
            "lanes": [{"lane_id": "NE-CODEX-01", "lane_control_heartbeat": "bad"}],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
            },
        },
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 1
    assert "continued_after_nonzero" not in receipt["actions"][0]


def test_dynamic_repair_fails_closed_after_nonzero_with_missing_lane_control_counter(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time(),
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "lane_control_substrate_blockers": [],
            "lanes": [{"lane_id": "NE-CODEX-01", "lane_control_heartbeat": {"heartbeat": {}}}],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
            },
        },
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 1
    assert "continued_after_nonzero" not in receipt["actions"][0]


def test_dynamic_repair_fails_closed_after_nonzero_with_top_level_lane_heartbeat_error(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time(),
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "lane_control_substrate_blockers": [],
            "lanes": [
                {
                    "lane_id": "NE-CODEX-01",
                    "lane_control_consecutive_failures": 0,
                    "lane_control_heartbeat": {"last_error_message": "sqlite refused heartbeat"},
                },
            ],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
            },
        },
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 1
    assert "continued_after_nonzero" not in receipt["actions"][0]


def test_dynamic_repair_fails_closed_after_nonzero_with_drifted_lane_heartbeat_error_key(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time(),
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "lane_control_substrate_blockers": [],
            "lanes": [
                {
                    "lane_id": "NE-CODEX-01",
                    "lane_control_consecutive_failures": 0,
                    "lane_control_heartbeat": {"error_message": "sqlite refused heartbeat"},
                },
            ],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
            },
        },
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 1
    assert "continued_after_nonzero" not in receipt["actions"][0]


def test_dynamic_repair_fails_closed_after_nonzero_with_lane_heartbeat_error(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time(),
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "lane_control_substrate_blockers": [],
            "lanes": [
                {
                    "lane_id": "NE-CODEX-01",
                    "lane_control_heartbeat": {"heartbeat": {"last_error_message": "sqlite refused heartbeat"}},
                },
            ],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
            },
        },
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 1
    assert "continued_after_nonzero" not in receipt["actions"][0]


def test_dynamic_repair_fails_closed_after_nonzero_even_if_stale_disk_proof_passes(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: {"proof_status": "PASS", "remaining_blockers": []},
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 1
    assert "continued_after_nonzero" not in receipt["actions"][0]


def test_dynamic_repair_fails_closed_after_nonzero_with_stale_restart_guard_proof(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": 1,
            "updated_at_epoch": time.time(),
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
            },
        },
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 1
    assert "continued_after_nonzero" not in receipt["actions"][0]


def test_dynamic_repair_fails_closed_after_nonzero_with_future_skewed_proof(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time() + 3600,
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
            },
        },
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 1
    assert "continued_after_nonzero" not in receipt["actions"][0]


def test_dynamic_repair_continues_with_same_second_truncated_fresh_epoch(tmp_path):
    calls: list[list[str]] = []
    proofs = iter([
        {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": int(time.time()),
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "lane_control_substrate_blockers": [],
            "lanes": [],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
            },
        },
        {"proof_status": "PASS", "remaining_blockers": []},
    ])

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            stdout = "proof attempted"
            stderr = ""

            @property
            def returncode(self):
                return 2 if len(calls) == 1 else 0

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: next(proofs),
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIRED_PASS"
    assert len(calls) == 2


def test_dynamic_repair_is_bounded_after_recoverable_restart_guard_nonzeros(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time(),
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "lane_control_substrate_blockers": [],
            "lanes": [],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
            },
        },
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_ATTEMPTED_FAILED"
    assert len(calls) == 3
    assert all(action["continued_after_nonzero"] is True for action in receipt["actions"])


def test_dynamic_repair_fails_closed_after_partial_continue_then_nonrecoverable_nonzero(tmp_path):
    calls: list[list[str]] = []
    proofs = iter([
        {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time() + 5,
            "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
            "lane_control_substrate_blockers": [],
            "lanes": [],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
            },
        },
        {
            "proof_status": "REPAIR_ATTEMPTED_FAILED",
            "checked_at_epoch": time.time(),
            "remaining_blockers": [
                "live_codex_build_lanes 0 < required 2",
                "PR_NOT_APPROVED",
            ],
            "lane_control_substrate_blockers": [],
            "lanes": [],
            "kanban_db_quick_check": {
                "status": "OK",
                "blocking": False,
                "db_path": "/tmp/hermes-home/kanban.db",
            },
        },
    ])

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: next(proofs),
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 2
    assert receipt["actions"][0]["continued_after_nonzero"] is True
    assert "continued_after_nonzero" not in receipt["actions"][1]


def test_dynamic_repair_fails_closed_when_second_nonzero_reuses_previous_proof(tmp_path):
    calls: list[list[str]] = []
    stale_epoch = time.time() + 1
    stale_proof = {
        "proof_status": "REPAIR_ATTEMPTED_FAILED",
        "checked_at_epoch": stale_epoch,
        "remaining_blockers": ["live_codex_build_lanes 0 < required 2"],
        "lane_control_substrate_blockers": [],
        "lanes": [],
        "kanban_db_quick_check": {
            "status": "OK",
            "blocking": False,
            "db_path": "/tmp/hermes-home/kanban.db",
        },
    }

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 2
            stdout = "proof attempted"
            stderr = ""

        return Result()

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["live_codex_build_lanes 0 < required 2"]},
        "neoengine",
        lambda: stale_proof,
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert len(calls) == 2
    assert receipt["actions"][0]["continued_after_nonzero"] is True
    assert "continued_after_nonzero" not in receipt["actions"][1]


def test_dynamic_repair_kill_switch_and_non_mutating_pass_path(tmp_path):
    calls: list[list[str]] = []
    passing = {"proof_status": "REPAIRED_PASS", "remaining_blockers": []}

    assert proof_is_passing(passing) is True
    receipt = run_dynamic_runtime_repair(
        passing,
        "neowealth",
        lambda: {"proof_status": "FAIL"},
        command_runner=lambda cmd, **kwargs: calls.append(cmd),
        hermes_home=tmp_path,
        enabled=True,
    )
    assert receipt["runtime_repair_status"] == "NOT_NEEDED"
    assert calls == []

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["down"]},
        "neowealth",
        lambda: {"proof_status": "PASS", "remaining_blockers": []},
        command_runner=lambda cmd, **kwargs: calls.append(cmd),
        hermes_home=tmp_path,
        enabled=False,
    )
    assert receipt["runtime_repair_status"] == "DISABLED"
    assert watchdog_runtime_status_after_repair(receipt) == "RUNTIME_REPAIR_DISABLED"
    assert calls == []


def test_dynamic_repair_fails_closed_when_runner_raises_timeout(tmp_path):
    def runner(cmd: list[str], **_kwargs):
        raise TimeoutError("repair command timed out")

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["down"]},
        "neowealth",
        lambda: {"proof_status": "PASS", "remaining_blockers": []},
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert watchdog_runtime_status_after_repair(receipt) == "RUNTIME_REPAIR_COMMAND_FAILED"
    assert receipt["actions"][0]["exception_type"] == "TimeoutError"
    assert receipt["final_proof_status"] == "REPAIR_ATTEMPTED_FAILED"


def test_dynamic_repair_fails_closed_when_proof_reload_raises(tmp_path):
    def runner(cmd: list[str], **_kwargs):
        class Result:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return Result()

    def load_latest_proof():
        raise RuntimeError("proof file unavailable")

    receipt = run_dynamic_runtime_repair(
        {"proof_status": "REPAIR_ATTEMPTED_FAILED", "remaining_blockers": ["down"]},
        "neowealth",
        load_latest_proof,
        command_runner=runner,
        hermes_home=tmp_path,
        enabled=True,
    )

    assert receipt["runtime_repair_status"] == "REPAIR_COMMAND_FAILED"
    assert receipt["actions"][0]["proof_reload_error"]["exception_type"] == "RuntimeError"
    assert receipt["final_proof_status"] == "REPAIR_ATTEMPTED_FAILED"


def test_lane_registration_preflight_catches_canonical_neowealth_unknown_lane_before_spawn():
    registered = {"codex", "nw-sonnet-01-fin-mvp-integration-recovery"}

    missing = missing_required_lane_registrations(registered)
    receipt = lane_registration_preflight_receipt(registered)

    assert missing == ["nw-codex-01-fin-mvp-runtime-recovery"]
    assert receipt["status"] == "UNKNOWN_DEV_LANE_REGISTRATION_MISSING"
    assert receipt["spawn_allowed"] is False
    assert receipt["missing_lane_ids"] == ["nw-codex-01-fin-mvp-runtime-recovery"]

    complete = lane_registration_preflight_receipt(CANONICAL_NEOWEALTH_RETAINED_LANES)
    assert complete["status"] == "PASS"
    assert complete["spawn_allowed"] is True
    assert Path(runtime_repair_commands("neowealth", hermes_home="/tmp/hermes-home")[1][1]).name == "neowealth-runtime-health-loop.py"
