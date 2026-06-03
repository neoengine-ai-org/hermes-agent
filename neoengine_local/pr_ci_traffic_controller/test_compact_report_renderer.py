#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import pathlib

HELPER_PATH = pathlib.Path(__file__).with_name("compact_report_renderer.py")
spec = importlib.util.spec_from_file_location("compact_report_renderer", HELPER_PATH)
mod = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(mod)  # type: ignore[union-attr]


def base_contract() -> dict:
    return {
        "repo": "synthetic/repo",
        "org": "synthetic-org",
        "drain_action_posture": "TRUE_NO_ACTION_SAFE",
        "hard_coded_example_check": "PASS",
        "lane_ticket_durable_mutation_proof": "N/A",
        "durable_mutation_readback_verified": "N/A",
        "lane_ticket_consumption_proof": "N/A",
        "frontier_consumer_proof": "N/A",
        "actions_performed": [],
        "actions_intentionally_withheld": [],
        "blocker_groups": [],
        "review_approval_pending": [],
        "held_prs": [],
        "required_next_action": "none",
        "escalation_status": "none",
        "full_report_path": "/synthetic/report.json",
        "open_pr_inventory": ["synthetic item should not render by default"],
        "controller_work_inbox": {"done": 1, "blocked": 0, "deferred": 0, "queued": 2, "top_action": "synthetic-item done", "next_item": "synthetic-next"},
    }


def test_compact_renderer_default_shape_and_omits_full_inventory() -> None:
    rendered = mod.render_compact_report(base_contract())
    lines = [line for line in rendered.splitlines() if line.strip()]
    assert 10 <= len(lines) <= 20
    assert "synthetic/repo" in rendered
    assert "Posture: TRUE_NO_ACTION_SAFE" in rendered
    assert "Proof:" in rendered
    assert "Full report: /synthetic/report.json" in rendered
    assert "Controller inbox: 1 done / 0 blocked / 0 deferred / 2 queued" in rendered
    assert "Inbox action: synthetic-item done" in rendered
    assert "Next inbox item: synthetic-next" in rendered
    assert "synthetic item should not render by default" not in rendered


def test_compact_renderer_expands_on_consumption_fail_but_stays_grouped() -> None:
    payload = base_contract()
    payload["drain_action_posture"] = "DRAIN_ACTION_ATTEMPTED_BUT_FAILED_CLOSED"
    payload["lane_ticket_consumption_proof"] = "FAIL"
    payload["blocker_groups"] = ["lane-ticket consumption proof failed"]
    rendered = mod.render_compact_report(payload)
    assert "Posture: DRAIN_ACTION_ATTEMPTED_BUT_FAILED_CLOSED" in rendered
    assert "consumption FAIL" in rendered
    assert "lane-ticket consumption proof failed" in rendered
    assert len([line for line in rendered.splitlines() if line.strip()]) <= 20


def test_compact_renderer_uses_helper_values_not_freeform_override() -> None:
    payload = base_contract()
    payload["drain_action_posture"] = "DRAIN_ACTION_ATTEMPTED_BUT_FAILED_CLOSED"
    payload["freeform_summary"] = "Posture: TRUE_NO_ACTION_SAFE"
    rendered = mod.render_compact_report(payload)
    assert "Posture: DRAIN_ACTION_ATTEMPTED_BUT_FAILED_CLOSED" in rendered
    assert "Posture: TRUE_NO_ACTION_SAFE" not in rendered


def test_compact_renderer_has_no_hardcoded_pr_ticket_examples() -> None:
    rendered = mod.render_compact_report(base_contract())
    forbidden = ["#", "live-" + "pr-marker", "live-" + "date-marker", "live-" + "neoengine-ticket-marker", "live-" + "neowealth-ticket-marker"]
    assert all(item not in rendered for item in forbidden)



def test_adversarial_review_mentions_review_lane_and_consumer_status() -> None:
    payload = base_contract()
    payload["required_next_action"] = "opposite-provider adversarial review for synthetic PR group"
    payload["escalation_status"] = "GPT-5.5 no; Opus queued with receipt"
    payload["adversarial_review_lane"] = "Opus"
    payload["adversarial_review_consumer_status"] = "queued"
    rendered = mod.render_compact_report(payload)
    assert "Review lane: Opus queued" in rendered
    assert "Escalation: GPT-5.5 no; Opus queued with receipt" in rendered


def test_adversarial_review_without_lane_renders_blocked_no_consumer_clarity() -> None:
    payload = base_contract()
    payload["required_next_action"] = "adversarial review required before drain can clear"
    payload["escalation_status"] = "GPT-5.5 no; Opus no"
    rendered = mod.render_compact_report(payload)
    assert "Review lane: review required but not yet routed; consumer blocked-no-consumer" in rendered


def test_compact_renderer_includes_tier1_authority_lines() -> None:
    payload = base_contract()
    payload.update({
        "tier1_local_actuator_enabled": True,
        "tier1_action": "replacement_pressure_ticket_create_update",
        "github_write_authority": "disabled",
        "next_permission_limited_action": "rerun GitHub checks",
    })
    rendered = mod.render_compact_report(payload)
    assert "Tier 1 local actuator: enabled" in rendered
    assert "Tier 1 action: replacement_pressure_ticket_create_update" in rendered
    assert "GitHub write authority: disabled" in rendered
    assert "Next permission-limited action: rerun GitHub checks" in rendered

if __name__ == "__main__":
    test_compact_renderer_default_shape_and_omits_full_inventory()
    test_compact_renderer_expands_on_consumption_fail_but_stays_grouped()
    test_compact_renderer_uses_helper_values_not_freeform_override()
    test_compact_renderer_has_no_hardcoded_pr_ticket_examples()
    test_adversarial_review_mentions_review_lane_and_consumer_status()
    test_adversarial_review_without_lane_renders_blocked_no_consumer_clarity()
    test_compact_renderer_includes_tier1_authority_lines()
    print("compact report renderer tests PASS")
