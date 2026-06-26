from __future__ import annotations

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
