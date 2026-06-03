#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import pathlib

HELPER_PATH = pathlib.Path(__file__).with_name("controller_contract.py")
spec = importlib.util.spec_from_file_location("controller_contract", HELPER_PATH)
mod = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(mod)  # type: ignore[union-attr]


def base_input() -> dict:
    return {
        "hard_coded_example_check": "PASS",
        "inbox_repair_result": {"parse_status": "OK", "classification": "LANE_TICKET_INBOX_OK", "trusted_consumer_receipts": True},
        "lane_ticket_durable_mutation_proof": "N/A",
        "durable_mutation_method": "none",
        "durable_mutation_readback_verified": "N/A",
        "lane_ticket_consumption_proof": "PASS",
        "frontier_consumer_proof": "N/A",
        "hold_behind_reconciliation_performed": "N/A",
        "ready_to_advance_candidates_checked": "YES",
        "live_pr_pressure_status": "clear",
        "actions": [],
        "action_attempted": False,
        "action_failed": False,
    }


def test_true_no_action_safe_when_all_required_proofs_pass_or_na_and_no_pressure() -> None:
    result = mod.derive_contract(base_input())
    assert result["drain_action_posture"] == "TRUE_NO_ACTION_SAFE"
    assert result["valid"] is True


def test_consumption_fail_forces_fail_closed_not_true_no_action_safe() -> None:
    payload = base_input()
    payload["lane_ticket_consumption_proof"] = "FAIL"
    result = mod.derive_contract(payload)
    assert result["drain_action_posture"] == "DRAIN_ACTION_ATTEMPTED_BUT_FAILED_CLOSED"
    assert "TRUE_NO_ACTION_SAFE_FORBIDDEN_FAILED_OR_UNVERIFIABLE_PROOF" in result["classifications"]


def test_malformed_inbox_failed_repair_forces_fail_closed_and_untrusted_receipts() -> None:
    payload = base_input()
    payload["inbox_repair_result"] = {
        "parse_status": "MALFORMED",
        "classification": "LANE_TICKET_INBOX_MALFORMED",
        "recovery_status": "FAILED",
        "trusted_consumer_receipts": False,
        "backup_path": "/synthetic/inbox.json.malformed.timestamp.bak",
    }
    payload["lane_ticket_consumption_proof"] = "FAIL"
    result = mod.derive_contract(payload)
    assert result["drain_action_posture"] == "DRAIN_ACTION_ATTEMPTED_BUT_FAILED_CLOSED"
    assert result["lane_ticket_consumption_proof"] == "FAIL"
    assert "LANE_TICKET_INBOX_MALFORMED" in result["classifications"]
    assert result["trusted_consumer_receipts"] is False
    assert "repair lane-ticket inbox before trusting consumer receipts" in result["next_action"]


def test_malformed_inbox_recovered_is_applied_and_uses_readback_proofs() -> None:
    payload = base_input()
    payload["inbox_repair_result"] = {
        "parse_status": "MALFORMED",
        "classification": "LANE_TICKET_INBOX_MALFORMED",
        "recovery_status": "RECOVERED",
        "trusted_consumer_receipts": True,
        "lane_ticket_durable_mutation_proof": "PASS",
        "durable_mutation_method": "structured_json_atomic_write",
        "durable_mutation_readback_verified": "YES",
    }
    payload["lane_ticket_consumption_proof"] = "PASS"
    result = mod.derive_contract(payload)
    assert result["drain_action_posture"] == "DRAIN_ACTION_APPLIED"
    assert result["lane_ticket_durable_mutation_proof"] == "PASS"
    assert result["durable_mutation_readback_verified"] == "YES"


def test_frontier_required_without_receipt_forces_fail_closed() -> None:
    payload = base_input()
    payload["frontier_consumer_proof"] = "FAIL"
    result = mod.derive_contract(payload)
    assert result["drain_action_posture"] == "DRAIN_ACTION_ATTEMPTED_BUT_FAILED_CLOSED"
    assert "frontier consumer proof failed" in result["failure_reasons"]


def test_green_review_pending_pressure_prevents_true_no_action_safe() -> None:
    payload = base_input()
    payload["live_pr_pressure_status"] = "review_pending"
    result = mod.derive_contract(payload)
    assert result["drain_action_posture"] == "DRAIN_ACTION_REQUIRED_BUT_NOT_APPLIED"
    assert "live PR pressure remains" in result["failure_reasons"]


def test_report_semantics_reject_bad_true_no_action_pair() -> None:
    problems = mod.validate_report_semantics({
        "drain_action_posture": "TRUE_NO_ACTION_SAFE",
        "lane_ticket_consumption_proof": "FAIL",
    })
    assert "TRUE_NO_ACTION_SAFE cannot appear with failed/unverifiable proofs" in problems


def test_compact_renderer_uses_contract_values() -> None:
    payload = base_input()
    payload["lane_ticket_consumption_proof"] = "FAIL"
    result = mod.derive_contract(payload)
    rendered = mod.render_compact_report("SyntheticRepo", result, "/synthetic/full-report.json")
    assert "Posture: DRAIN_ACTION_ATTEMPTED_BUT_FAILED_CLOSED" in rendered
    assert "consumption FAIL" in rendered
    assert "TRUE_NO_ACTION_SAFE" not in rendered.splitlines()[2]


def test_no_inbox_no_required_tickets_allows_consumption_na() -> None:
    payload = base_input()
    payload["inbox_repair_result"] = {"parse_status": "MISSING", "classification": "LANE_TICKET_INBOX_MISSING", "trusted_consumer_receipts": False}
    payload["lane_ticket_consumption_proof"] = "N/A"
    result = mod.derive_contract(payload)
    assert result["lane_ticket_consumption_proof"] == "N/A"
    assert result["drain_action_posture"] == "TRUE_NO_ACTION_SAFE"


def test_durable_mutation_method_none_with_proof_pass_is_rejected() -> None:
    problems = mod.validate_report_semantics({
        "drain_action_posture": "TRUE_NO_ACTION_SAFE",
        "lane_ticket_durable_mutation_proof": "PASS",
        "durable_mutation_method": "none",
        "durable_mutation_readback_verified": "N/A",
        "lane_ticket_consumption_proof": "PASS",
    })
    assert "durable mutation proof PASS requires structured_json_atomic_write" in problems


def test_failed_durable_mutation_drops_create_update_supersede_success_claims() -> None:
    payload = base_input()
    payload.update({
        "durable_mutation_attempted": True,
        "lane_ticket_durable_mutation_proof": "FAIL",
        "durable_mutation_method": "structured_json_atomic_write",
        "durable_mutation_readback_verified": "NO",
        "actions_performed": ["LANE_TICKET_CREATED", "OBSERVATION_RECORDED"],
    })
    result = mod.derive_contract(payload)
    assert result["drain_action_posture"] == "DRAIN_ACTION_ATTEMPTED_BUT_FAILED_CLOSED"
    assert "LANE_TICKET_CREATED" not in result["actions_performed"]
    assert result["durable_mutation_readback_verified"] == "NO"


def test_done_lane_action_with_current_head_ci_failure_requires_replacement_coverage() -> None:
    payload = base_input()
    payload.update({
        "lane_action_done": True,
        "live_pr_pressure_status": "current_head_ci_failure",
    })
    result = mod.derive_contract(payload)
    assert result["drain_action_posture"] == "DRAIN_ACTION_REQUIRED_BUT_NOT_APPLIED"
    assert "REPLACEMENT_PRESSURE_COVERAGE_REQUIRED" in result["classifications"]


def test_event_driven_frontier_required_cannot_stop_at_normal_lane_ticket_only() -> None:
    payload = base_input()
    payload.update({
        "frontier_escalation_required": True,
        "actions_performed": ["LANE_TICKET_CREATED"],
    })
    result = mod.derive_contract(payload)
    assert result["frontier_consumer_proof"] == "FAIL"
    assert result["drain_action_posture"] == "DRAIN_ACTION_APPLIED" or result["drain_action_posture"] == "DRAIN_ACTION_ATTEMPTED_BUT_FAILED_CLOSED"
    assert "FRONTIER_CONSUMER_PROOF_REQUIRED" in result["classifications"]
    assert "event-driven frontier escalation cannot stop at a normal lane ticket" in result["invalid_combinations_rejected"]


def test_local_report_only_output_cannot_be_drain_action_applied() -> None:
    payload = base_input()
    payload["actions_performed"] = ["OBSERVATION_RECORDED"]
    result = mod.derive_contract(payload)
    assert result["drain_action_posture"] == "DRAIN_ACTION_REQUIRED_BUT_NOT_APPLIED"
    assert "LOCAL_REPORT_ONLY_NO_PRESSURE_RELEASE" in result["classifications"]


def test_permission_withheld_pressure_gets_distinct_posture() -> None:
    payload = base_input()
    payload.update({
        "live_pr_pressure_status": "current_head_ci_failure",
        "actions_intentionally_withheld": ["no merge/auto-merge/rerun in script-first wrapper without explicit runtime authority"],
    })
    result = mod.derive_contract(payload)
    assert result["drain_action_posture"] == "ACTION_WITHHELD_PERMISSION_LIMIT"



def test_adversarial_review_next_action_requires_lane_and_consumer_status() -> None:
    payload = base_input()
    payload["required_next_action"] = "opposite-provider adversarial review before advancing"
    result = mod.derive_contract(payload)
    assert result["drain_action_posture"] == "DRAIN_ACTION_ATTEMPTED_BUT_FAILED_CLOSED"
    assert result["frontier_consumer_proof"] == "FAIL"
    assert "ADVERSARIAL_REVIEW_ROUTE_UNSPECIFIED" in result["classifications"]
    assert "adversarial review mention requires review lane and consumer status" in result["invalid_combinations_rejected"]


def test_adversarial_review_with_opus_receipt_is_validly_labeled() -> None:
    payload = base_input()
    payload.update({
        "required_next_action": "opposite-provider adversarial review before advancing",
        "adversarial_review_lane": "Opus",
        "adversarial_review_consumer_status": "queued",
        "escalation_status": "GPT-5.5 no; Opus queued with receipt",
        "frontier_consumer_proof": "PASS",
    })
    result = mod.derive_contract(payload)
    assert result["adversarial_review_lane"] == "Opus"
    assert result["adversarial_review_consumer_status"] == "queued"
    assert result["valid"] is True


def test_successful_tier1_replacement_mutation_yields_applied() -> None:
    payload = base_input()
    payload.update({
        "live_pr_pressure_status": "current_head_ci_failure",
        "tier1_local_actuator_enabled": True,
        "action_authority_matrix": {"tier1": {"enabled": True}, "tier2": {"enabled": False}, "tier3": {"enabled": False}},
        "inbox_repair_result": {"parse_status": "OK", "classification": "LANE_TICKET_INBOX_OK", "trusted_consumer_receipts": True, "lane_ticket_durable_mutation_proof": "N/A", "durable_mutation_readback_verified": "N/A"},
        "actions_performed": ["REPLACEMENT_PRESSURE_TICKET_CREATED"],
        "tier1_actions_applied": ["replacement_pressure_ticket_create_update"],
        "durable_mutation_attempted": True,
        "lane_ticket_durable_mutation_proof": "PASS",
        "durable_mutation_method": "structured_json_atomic_write",
        "durable_mutation_readback_verified": "YES",
    })
    result = mod.derive_contract(payload)
    assert result["drain_action_posture"] == "DRAIN_ACTION_APPLIED"
    assert result["lane_ticket_durable_mutation_proof"] == "PASS"
    assert result["durable_mutation_readback_verified"] == "YES"
    assert result["tier1_local_actuator_enabled"] is True
    assert result["github_write_authority"] == "disabled"


def test_failed_tier1_mutation_yields_attempted_failed_closed() -> None:
    payload = base_input()
    payload.update({
        "live_pr_pressure_status": "current_head_ci_failure",
        "tier1_local_actuator_enabled": True,
        "tier1_actions_attempted": ["replacement_pressure_ticket_create_update"],
        "durable_mutation_attempted": True,
        "lane_ticket_durable_mutation_proof": "FAIL",
        "durable_mutation_method": "structured_json_atomic_write",
        "durable_mutation_readback_verified": "NO",
    })
    result = mod.derive_contract(payload)
    assert result["drain_action_posture"] == "DRAIN_ACTION_ATTEMPTED_BUT_FAILED_CLOSED"


def test_only_github_write_actions_available_yields_permission_limit() -> None:
    payload = base_input()
    payload.update({
        "live_pr_pressure_status": "current_head_ci_failure",
        "tier1_local_actuator_enabled": True,
        "tier1_actions_evaluated": ["lane_ticket_repair_update"],
        "tier1_actions_withheld": [{"action": "lane_ticket_repair_update", "reason": "no safe local mutation applies"}],
        "tier2_tier3_actions_required_but_disabled": ["rerun GitHub checks"],
        "actions_intentionally_withheld": ["GitHub write authority disabled: rerun GitHub checks"],
    })
    result = mod.derive_contract(payload)
    assert result["drain_action_posture"] == "ACTION_WITHHELD_PERMISSION_LIMIT"
    assert result["next_permission_limited_action"] == "rerun GitHub checks"


def test_ready_to_advance_local_receipt_keeps_permission_limited_posture_without_github_write() -> None:
    payload = base_input()
    payload.update({
        "live_pr_pressure_status": "ready_to_advance_permission_limit",
        "tier1_local_actuator_enabled": True,
        "actions_performed": ["READY_TO_ADVANCE_LOCAL_RECEIPT"],
        "tier1_actions_applied": ["ready_to_advance_local_receipt"],
        "durable_mutation_attempted": True,
        "lane_ticket_durable_mutation_proof": "PASS",
        "durable_mutation_method": "structured_json_atomic_write",
        "durable_mutation_readback_verified": "YES",
        "actions_intentionally_withheld": ["GitHub write authority disabled: auto-merge arm"],
        "tier2_tier3_actions_required_but_disabled": ["auto-merge arm"],
    })
    result = mod.derive_contract(payload)
    assert result["drain_action_posture"] == "ACTION_WITHHELD_PERMISSION_LIMIT"
    assert result["github_write_authority"] == "disabled"
    assert "auto-merge arm" in result["next_permission_limited_action"]

if __name__ == "__main__":
    test_true_no_action_safe_when_all_required_proofs_pass_or_na_and_no_pressure()
    test_consumption_fail_forces_fail_closed_not_true_no_action_safe()
    test_malformed_inbox_failed_repair_forces_fail_closed_and_untrusted_receipts()
    test_malformed_inbox_recovered_is_applied_and_uses_readback_proofs()
    test_frontier_required_without_receipt_forces_fail_closed()
    test_green_review_pending_pressure_prevents_true_no_action_safe()
    test_report_semantics_reject_bad_true_no_action_pair()
    test_compact_renderer_uses_contract_values()
    test_no_inbox_no_required_tickets_allows_consumption_na()
    test_durable_mutation_method_none_with_proof_pass_is_rejected()
    test_failed_durable_mutation_drops_create_update_supersede_success_claims()
    test_done_lane_action_with_current_head_ci_failure_requires_replacement_coverage()
    test_event_driven_frontier_required_cannot_stop_at_normal_lane_ticket_only()
    test_local_report_only_output_cannot_be_drain_action_applied()
    test_permission_withheld_pressure_gets_distinct_posture()
    test_adversarial_review_next_action_requires_lane_and_consumer_status()
    test_adversarial_review_with_opus_receipt_is_validly_labeled()
    test_successful_tier1_replacement_mutation_yields_applied()
    test_failed_tier1_mutation_yields_attempted_failed_closed()
    test_only_github_write_actions_available_yields_permission_limit()
    test_ready_to_advance_local_receipt_keeps_permission_limited_posture_without_github_write()
    print("controller contract tests PASS")
