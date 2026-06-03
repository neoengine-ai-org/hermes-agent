#!/usr/bin/env python3
"""Validate shared PR/CI controller prompt invariants requested by Board.

This is an offline prompt/config validator: it proves the shared control file now
contains the durable reporting and pressure-ledger invariants that both thin cron
bootstraps load at runtime.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CONTROL = Path(__file__).with_name("neoengine-product-org-shared-cron-control.md")
TEXT = CONTROL.read_text(encoding="utf-8")

REQUIRED_SUBSTRINGS = {
    "compact_default": "Default compact PR/CI controller report rule",
    "normal_target": "Normal user-facing response target: 10-20 lines",
    "max_grouped_bullets": "no more than 5 grouped bullets",
    "full_report_path": "Full report: <path>",
    "report_directory": "/Users/neoengine/.hermes/state/pr-ci-traffic-controller/reports/",
    "split_ledgers": "Lane-ticket execution status and live PR pressure status are separate ledgers",
    "done_not_clear": "lane-ticket execution status = done and live PR pressure status != clear/merged/closed/superseded",
    "replacement_coverage": "replacement pressure coverage: PRESENT / CREATED / MISSING",
    "green_review_pending": "Green review-pending PRs require active pressure coverage",
    "current_head_failure": "Current-head failure replacement rule",
    "ready_to_advance": "Ready-to-advance permission-limit rule",
    "compact_shape": "<Repo> PR/CI controller — 20m tick",
    "proof_line": "Proof: hard-coded <PASS/FAIL> · mutation <PASS/FAIL/N/A> · consumption <PASS/FAIL> · frontier <PASS/FAIL/N/A>",
    "expand_triggers": "Compact report expansion triggers",
    "full_report_every_open_pr": "For every open PR in the full report, include",
    "no_done_without_pressure": "Do not say `done` without live pressure context",
    "no_merge_without_authority": "no merge/auto-merge unless explicitly authorized",
    "true_no_action_safe_proof_gate": "TRUE_NO_ACTION_SAFE proof gate",
    "true_no_action_forbidden_failed_unverifiable": "`TRUE_NO_ACTION_SAFE` is forbidden if any required proof is `FAIL`, unverifiable",
    "true_no_action_requires_all_applicable_pass_na": "requires all applicable proofs to be PASS or N/A",
    "consumption_na_only_without_inbox": "lane-ticket consumption proof: PASS or N/A only when no lane-ticket inbox exists and no tickets are required",
    "malformed_inbox_rule": "Malformed lane-ticket inbox rule",
    "malformed_classification": "LANE_TICKET_INBOX_MALFORMED",
    "malformed_forces_consumption_fail": "set `Lane-ticket consumption proof: FAIL`",
    "malformed_forces_fail_closed_or_repaired": "set `Drain action posture: DRAIN_ACTION_ATTEMPTED_BUT_FAILED_CLOSED` unless bounded recovery succeeds",
    "no_owning_lanes_when_malformed": "do not use owning-lane delegation as a safety claim",
    "no_trust_receipts_when_malformed": "do not trust consumer receipts from that inbox",
    "no_normal_tickets_until_repaired": "do not create/update normal lane tickets until the inbox is repaired",
    "malformed_repair_rule": "Malformed inbox repair rule",
    "backup_before_repair": "copy it to a timestamped `.malformed.<timestamp>.bak` path",
    "recover_non_json_preface": "non-JSON preface followed by one valid JSON object",
    "structured_atomic_recovered_write": "write it using the structured JSON atomic helper or equivalent atomic replace",
    "reread_parse_verify": "re-read and parse; verify durable readback",
    "malformed_compact_report_rule": "Malformed inbox compact report rule",
    "compact_next_repair_inbox": "Next: repair inbox before trusting lane consumer receipts",
    "deterministic_helpers_section": "Deterministic controller contract helpers",
    "inbox_repair_helper_path": "/Users/neoengine/.hermes/state/pr-ci-traffic-controller/lane_ticket_inbox_repair.py",
    "controller_contract_helper_path": "/Users/neoengine/.hermes/state/pr-ci-traffic-controller/controller_contract.py",
    "compact_renderer_helper_path": "/Users/neoengine/.hermes/state/pr-ci-traffic-controller/compact_report_renderer.py",
    "llm_summarizes_not_derives": "The LLM summarizes and routes from structured helper output",
    "contract_output_authoritative": "Use the contract output as authoritative",
    "contract_semantic_validation_fail_closed": "If contract semantic validation returns errors, fail closed",
    "compact_from_structured_contract": "Render compact `Posture`, `Proof`, `Action`, `Blocked`, `Review/approval pending`, `Held`, `Next`, `Escalation`, and `Full report`",
    "compact_default_10_20": "Default output target is 10-20 lines",
    "never_override_helper_values": "Never override deterministic posture/proof/action values in prose",
    "deterministic_contract_invariant": "Deterministic contract invariant",
    "method_none_pass_forbidden": "Durable mutation proof PASS is forbidden when method is none",
    "done_lane_cannot_clear_pressure": "Done lane actions cannot clear live PR pressure",
    "frontier_normal_ticket_forbidden": "event-driven frontier escalation cannot stop at a normal lane ticket",
    "local_report_only_not_release": "local report-only output cannot count as pressure release",
    "tier1_target_posture": "CROSS_ORG_54_MINI_PR_CI_TRAFFIC_CONTROLLER_TIER_1_LOCAL_ACTUATOR_ENABLED",
    "authority_matrix": "Runtime action authority matrix",
    "bounded_local_actuator": "The controller may be a bounded local actuator. It must not be a GitHub writer",
    "tier1_enabled": "Tier 1 — enabled now as script-first local actuator authority",
    "lane_ticket_repair_update": "lane_ticket_repair_update",
    "replacement_pressure_ticket_create_update": "replacement_pressure_ticket_create_update",
    "stale_ticket_supersede": "stale_ticket_supersede",
    "dispatcher_nudge_with_no_duplicate_owner_proof": "dispatcher_nudge_with_no_duplicate_owner_proof",
    "ready_to_advance_local_receipt": "ready_to_advance_local_receipt",
    "tier2_disabled": "Tier 2 — disabled unless explicit GitHub metadata write authority is later granted",
    "tier3_disabled": "Tier 3 — disabled unless separately authorized",
    "github_write_disabled_row": "GitHub write authority: disabled",
    "tier1_compact_row": "Tier 1 local actuator: enabled",
    "tier1_full_report": "Tier 1 actions evaluated; Tier 1 actions applied; Tier 1 actions withheld and why",
    "no_duplicate_owner_proof": "no-duplicate-owner proof",
    "ready_receipt_no_github_writes": "must not perform merge, auto-merge, labels, comments, or other GitHub writes",
    "explicit_cron_bootstrap": "Both traffic-controller cron jobs must invoke `pr-ci-traffic-controller-runtime-wrapper.py` with explicit `script_args`",
    "neoengine_explicit_org_args": "--org neoengine --repo neoengine-ai-org/neoengine --workdir /Users/neoengine/workspace/ai-org/neoengine",
    "neowealth_explicit_org_args": "--org neowealth --repo neoengine-ai-org/neowealth --workdir /Users/neoengine/workspace/ai-org/product-orgs/neowealth",
    "org_resolution_order": "Wrapper org resolution order: explicit `--org`, explicit job/env org, explicit `--workdir`, cwd inference, otherwise fail closed",
    "watchdog_controller_handoff_posture": "CROSS_ORG_WATCHDOG_TO_PR_CI_CONTROLLER_INBOX_HANDOFF_INSTALLED",
    "controller_work_inbox_helper": "/Users/neoengine/.hermes/state/pr-ci-traffic-controller/controller_work_inbox.py",
    "neoengine_controller_work_inbox_path": "/Users/neoengine/.hermes/state/pr-ci-traffic-controller/neoengine-controller-work-inbox.json",
    "neowealth_controller_work_inbox_path": "/Users/neoengine/.hermes/state/pr-ci-traffic-controller/neowealth-controller-work-inbox.json",
    "controller_work_inbox_schema": "hermes.pr-ci-controller-work-inbox.v1",
    "watchdog_created_updated_line": "controller inbox item created/updated: <work_item_id>",
    "controller_consume_at_tick_start": "at the start of every controller cron tick",
    "queued_not_silent": "A queued item may not be silently ignored",
    "branch_namespace_operator_class": "BRANCH_NAMESPACE_COLLISION_REQUIRES_OPERATOR",
    "controller_inbox_compact_counts": "Controller inbox: <done/blocked/deferred/queued counts>",
    "inbox_action_compact_row": "Inbox action: <top item action or none>",
    "next_inbox_item_compact_row": "Next inbox item: <work_item_id or none>",
    "branch_collision_structured_evidence": "For every `branch_namespace_collision` item, the watchdog producer must include structured repair evidence",
    "branch_collision_colliding_ref": "`colliding_ref`",
    "branch_collision_requested_ref": "`requested_ref`",
    "branch_collision_git_show_ref": "`git_show_ref_evidence`",
    "branch_collision_open_pr_check": "`open_pr_uses_colliding_ref`: `YES`, `NO`, or `UNKNOWN`",
    "branch_collision_unpushed_check": "`unpushed_commits_present`: `YES`, `NO`, or `UNKNOWN`",
    "branch_collision_checkout_check": "`canonical_checkout_dirty_or_detached`: `YES` or `NO`",
    "branch_collision_safe_archive": "`safe_to_archive`: `YES`, `NO`, or `UNKNOWN`",
    "branch_collision_archive_candidate": "`archive_candidate_ref`",
    "branch_collision_unknown_blocks": "If any structured evidence field is `UNKNOWN`",
    "branch_collision_safe_direction": "open_pr_uses_colliding_ref=NO",
}

STATUS_VALUES = [
    "queued", "claimed", "in_progress", "parked", "blocked", "done", "superseded",
    "current_head_ci_failure", "body_contract_failure", "governance_failure",
    "merge_conflict", "stale_branch", "review_pending",
    "adversarial_review_pending", "approval_permission_limit", "overlap_hold",
    "draft_blocked", "runner_blocked", "ready_to_advance_permission_limit",
    "outside_controller_authority",
]

FORBIDDEN_HARDCODED_EXAMPLES = [
    "#" + suffix for suffix in ["520", "521", "510", "513", "524"]
]


def report_semantic_failures(report: str) -> list[str]:
    problems: list[str] = []
    has_true_no_action = "TRUE_NO_ACTION_SAFE" in report
    has_consumption_fail = "Lane-ticket consumption proof: FAIL" in report or "consumption FAIL" in report
    has_parse_failed = "lane-ticket inbox parse failed" in report or "non-JSON preface" in report or "LANE_TICKET_INBOX_MALFORMED" in report
    if has_true_no_action and has_consumption_fail:
        problems.append("TRUE_NO_ACTION_SAFE cannot appear with Lane-ticket consumption proof: FAIL")
    if has_true_no_action and has_parse_failed:
        problems.append("TRUE_NO_ACTION_SAFE cannot appear when lane-ticket inbox parse failed")
    if has_parse_failed and "LANE_TICKET_INBOX_MALFORMED" not in report:
        problems.append("malformed inbox state requires LANE_TICKET_INBOX_MALFORMED")
    if has_parse_failed and not ("DRAIN_ACTION_ATTEMPTED_BUT_FAILED_CLOSED" in report or "DRAIN_ACTION_APPLIED" in report):
        problems.append("malformed inbox requires repair-applied or fail-closed posture")
    if has_parse_failed and "current pressure is better handled by owning lanes" in report:
        problems.append("malformed inbox compact output cannot defer to owning lanes")
    return problems


def main() -> int:
    failures: list[str] = []
    for name, needle in REQUIRED_SUBSTRINGS.items():
        if needle not in TEXT:
            failures.append(f"missing required prompt invariant: {name}: {needle}")
    for value in STATUS_VALUES:
        if value not in TEXT:
            failures.append(f"missing status value: {value}")
    for pattern in FORBIDDEN_HARDCODED_EXAMPLES:
        if re.search(pattern, TEXT):
            failures.append(f"hard-coded PR example introduced: {pattern}")
    pair_forbidden = [
        ("TRUE_NO_ACTION_SAFE", "Lane-ticket consumption proof: FAIL"),
        ("TRUE_NO_ACTION_SAFE", "lane-ticket inbox parse failed"),
    ]
    # The prompt may mention forbidden pairs only as explicit prohibitions, not as allowed report examples.
    required_prohibitions = [
        "If the report has `Lane-ticket consumption proof: FAIL`, it cannot also claim `TRUE_NO_ACTION_SAFE`",
        "If the lane-ticket inbox parse failed or any required proof is failed/unverifiable, `TRUE_NO_ACTION_SAFE` is forbidden",
    ]
    for phrase in required_prohibitions:
        if phrase not in TEXT:
            failures.append(f"missing explicit forbidden-pair rule: {phrase}")
    if re.search(r"TRUE_NO_ACTION_SAFE[^\n]{0,120}Lane-ticket consumption proof: FAIL", TEXT) and required_prohibitions[0] not in TEXT:
        failures.append("TRUE_NO_ACTION_SAFE appears with consumption FAIL outside an explicit prohibition")
    if "current pressure is better handled by owning lanes" in TEXT:
        failures.append("forbidden weak malformed-inbox language remains: owning lanes should handle it")
    semantic_cases = {
        "invalid_true_no_action_with_consumption_fail": (
            "Posture: TRUE_NO_ACTION_SAFE\nLane-ticket consumption proof: FAIL\n",
            ["TRUE_NO_ACTION_SAFE cannot appear with Lane-ticket consumption proof: FAIL"],
        ),
        "invalid_true_no_action_with_parse_failed": (
            "Posture: TRUE_NO_ACTION_SAFE\nReason: lane-ticket inbox parse failed\n",
            ["TRUE_NO_ACTION_SAFE cannot appear when lane-ticket inbox parse failed", "malformed inbox state requires LANE_TICKET_INBOX_MALFORMED", "malformed inbox requires repair-applied or fail-closed posture"],
        ),
        "valid_malformed_fail_closed": (
            "Posture: DRAIN_ACTION_ATTEMPTED_BUT_FAILED_CLOSED\nClassification: LANE_TICKET_INBOX_MALFORMED\nLane-ticket consumption proof: FAIL\nAction: backed up malformed inbox; repair failed\n",
            [],
        ),
        "invalid_malformed_owning_lanes": (
            "Posture: DRAIN_ACTION_ATTEMPTED_BUT_FAILED_CLOSED\nClassification: LANE_TICKET_INBOX_MALFORMED\nLane-ticket consumption proof: FAIL\nReason: current pressure is better handled by owning lanes\n",
            ["malformed inbox compact output cannot defer to owning lanes"],
        ),
    }
    for name, (sample, expected) in semantic_cases.items():
        got = report_semantic_failures(sample)
        for e in expected:
            if e not in got:
                failures.append(f"semantic test {name} did not flag: {e}")
        unexpected = [g for g in got if g not in expected]
        if unexpected:
            failures.append(f"semantic test {name} unexpected flags: {unexpected}")
    for section in [
        "## Job section: pr-ci-traffic-controller / NeoEngine",
        "## Job section: pr-ci-traffic-controller / NeoWealth",
    ]:
        if section not in TEXT:
            failures.append(f"missing job section: {section}")
    if failures:
        print("FAIL: shared PR/CI controller prompt validation")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("PASS: shared PR/CI controller prompt validation")
    print(f"validated: {CONTROL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
