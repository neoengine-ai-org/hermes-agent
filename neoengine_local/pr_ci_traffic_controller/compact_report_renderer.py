#!/usr/bin/env python3
"""Compact renderer for PR/CI traffic-controller deterministic contract output."""
from __future__ import annotations

import argparse
import json
import pathlib
import re
from typing import Any

EXPAND_TRIGGERS = {
    "proof_fail",
    "durable_mutation_fail",
    "consumption_fail",
    "frontier_activity",
    "hard_coded_fail",
    "protected_boundary_risk",
    "ready_to_advance_action",
    "user_requested_full_detail",
}


def _join(values: Any, empty: str = "none") -> str:
    if not values:
        return empty
    if isinstance(values, str):
        return values
    return "; ".join(str(v) for v in values[:5])


def _mentions_adversarial_review(contract: dict[str, Any]) -> bool:
    haystack = " ".join(str(contract.get(k) or "") for k in ("required_next_action", "next_action", "escalation_status"))
    return bool(re.search(r"\b(adversarial review|opposite-provider review)\b", haystack, re.I))


def _review_lane_status(contract: dict[str, Any]) -> str | None:
    if not _mentions_adversarial_review(contract):
        return None
    lane = contract.get("adversarial_review_lane")
    status = contract.get("adversarial_review_consumer_status")
    if lane and status:
        return f"{lane} {status}"
    if status:
        return f"lane unspecified; consumer {status}"
    return "review required but not yet routed; consumer blocked-no-consumer"


def should_expand(contract: dict[str, Any]) -> bool:
    actions = set(contract.get("actions_performed") or [])
    if contract.get("hard_coded_example_check") == "FAIL":
        return True
    if contract.get("lane_ticket_durable_mutation_proof") == "FAIL":
        return True
    if contract.get("lane_ticket_consumption_proof") == "FAIL":
        return True
    if contract.get("frontier_consumer_proof") == "FAIL" or any(str(a).startswith("FRONTIER_REVIEW_") for a in actions):
        return True
    if contract.get("protected_boundary_risk"):
        return True
    if "READY_TO_ADVANCE_ACTION_APPLIED" in actions:
        return True
    if _mentions_adversarial_review(contract):
        return True
    if contract.get("user_requested_full_detail"):
        return True
    return False


def render_compact_report(contract: dict[str, Any]) -> str:
    repo = contract.get("repo") or contract.get("org") or "Repo"
    org = contract.get("org")
    title = f"{repo} PR/CI controller — 20m tick" if not org else f"{org}/{repo} PR/CI controller — 20m tick"
    proof = (
        f"hard-coded {contract.get('hard_coded_example_check', 'PASS')} · "
        f"mutation {contract.get('lane_ticket_durable_mutation_proof', 'N/A')} · "
        f"readback {contract.get('durable_mutation_readback_verified', 'N/A')} · "
        f"consumption {contract.get('lane_ticket_consumption_proof', 'N/A')} · "
        f"frontier {contract.get('frontier_consumer_proof', 'N/A')}"
    )
    actions = contract.get("actions_performed") or []
    if actions:
        action_summary = _join(actions)
    elif "LANE_TICKET_INBOX_MALFORMED" in set(contract.get("classifications") or []):
        action_summary = "malformed inbox backup attempted; recovery failed"
    else:
        action_summary = "none durably applied"
    review_lane = _review_lane_status(contract)
    controller_inbox = contract.get("controller_work_inbox") or {}
    tier1_enabled = "enabled" if contract.get("tier1_local_actuator_enabled") else "disabled"
    lines = [
        title,
        f"Posture: {contract.get('drain_action_posture')}",
        f"Proof: {proof}",
        f"Tier 1 local actuator: {tier1_enabled}",
        f"Tier 1 action: {contract.get('tier1_action') or 'none'}",
        f"GitHub write authority: {contract.get('github_write_authority') or 'disabled'}",
        f"Next permission-limited action: {contract.get('next_permission_limited_action') or 'none'}",
        f"Action: {action_summary}",
        f"Controller inbox: {controller_inbox.get('done', 0)} done / {controller_inbox.get('blocked', 0)} blocked / {controller_inbox.get('deferred', 0)} deferred / {controller_inbox.get('queued', 0)} queued",
        f"Inbox action: {controller_inbox.get('top_action') or 'none'}",
        f"Next inbox item: {controller_inbox.get('next_item') or 'none'}",
        f"Blocked: {_join(contract.get('blocker_groups') or contract.get('failure_reasons'))}",
        f"Review/approval pending: {_join(contract.get('review_approval_pending'))}",
        f"Held: {_join(contract.get('held_prs'))}",
        f"Next: {contract.get('required_next_action') or contract.get('next_action') or 'none'}",
    ]
    if review_lane:
        lines.append(f"Review lane: {review_lane}")
    lines.extend([
        f"Escalation: {contract.get('escalation_status') or 'none'}",
        f"Full report: {contract.get('full_report_path') or '<path>'}",
    ])
    if should_expand(contract):
        withheld = _join(contract.get("actions_intentionally_withheld"))
        classifications = _join(contract.get("classifications"))
        lines.insert(8, f"Withheld: {withheld}")
        lines.insert(9, f"Classified: {classifications}")
    # Default must remain compact. If future fields overfill, keep head plus required tail.
    if len(lines) > 20:
        lines = lines[:17] + lines[-3:]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=pathlib.Path)
    args = parser.parse_args()
    print(render_compact_report(json.loads(args.input.read_text())), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
