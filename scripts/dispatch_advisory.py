#!/usr/bin/env python3
"""Emit advisory Hermes dispatcher recommendations from classifier artifacts.

This is intentionally advisory-only. It consumes V1 classifier/review/downstream
artifacts and writes hermes-dispatch-advisory.json / .md without assigning agents,
blocking merges, mutating accepted state, or claiming merge/protected authority.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

SCHEMA_VERSION = "hermes-dispatch-advisory.v1"
NON_CLAIMS = [
    "not_dispatcher_enforced",
    "not_downstream_matrix_enforced",
    "not_cross_repo_propagated",
    "not_identity_provider_or_evidence_url_verified",
    "not_protected_approval",
    "not_production_readiness",
    "not_launch_readiness",
    "not_customer_data_readiness",
    "not_auto_merge_authority",
    "not_roadmap_os_accepted_state_mutation",
]
MECHANICAL_MARKERS = (
    "ruff",
    "lint",
    "format",
    "py_compile",
    "typecheck",
    "test",
    "compile",
    "mechanical",
    "merge_repair",
)
OPERATOR_MARKERS = (
    "artifact_missing",
    "artifact_invalid",
    "human_gate",
    "protected",
    "founder",
    "security",
    "finance",
)


def _list_value(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value is None:
        return []
    text = str(value).strip()
    if not text or text.lower() in {"none", "n/a", "na"}:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "1"}


def _sha_from_classification(classification: dict[str, object], key: str) -> str:
    direct = classification.get(key)
    if direct:
        return str(direct)
    execution = classification.get("classifier_execution")
    if isinstance(execution, dict):
        execution_data = cast(dict[str, object], execution)
        if execution_data.get(key):
            return str(execution_data[key])
    return "unknown"


def _missing_artifact_conditions(
    review_receipts: dict[str, object] | None,
    downstream_matrix: dict[str, object] | None,
) -> list[str]:
    conditions: list[str] = []
    if review_receipts is None:
        conditions.append("artifact_missing:review-receipts.json")
    if downstream_matrix is None:
        conditions.append("artifact_missing:downstream-matrix.json")
    return conditions


def _extract_stale_reviews(invalid_reasons: list[str]) -> list[str]:
    prefix = "stale_review_receipt:"
    return sorted({reason[len(prefix) :] for reason in invalid_reasons if reason.startswith(prefix)})


def _has_request_changes(invalid_reasons: list[str]) -> bool:
    return any("REQUEST_CHANGES" in reason or "unresolved_blockers" in reason for reason in invalid_reasons)


def _repeated_receipt_loop(review_receipts: dict[str, object] | None, invalid_reasons: list[str]) -> bool:
    if not review_receipts:
        return False
    receipts = review_receipts.get("receipts", [])
    if not isinstance(receipts, list):
        return False
    counts: dict[str, int] = {}
    for receipt in receipts:
        if isinstance(receipt, dict):
            receipt_data = cast(dict[str, object], receipt)
            review_type = str(receipt_data.get("review_type", "unknown"))
            counts[review_type] = counts.get(review_type, 0) + 1
    repeated_type = any(count >= 2 for count in counts.values())
    repeated_reason = any(reason.startswith(("stale_review_receipt:", "blocking_verdict:", "unresolved_blockers:")) for reason in invalid_reasons)
    return repeated_type and repeated_reason


def _support_or_adversarial_without_payload(classification: dict[str, object]) -> bool:
    surfaces = set(_list_value(classification.get("impacted_surfaces", [])))
    lanes = set(_list_value(classification.get("required_ci_lanes", [])))
    support_or_adversarial = bool({"support_customer_ops", "marketing_claims"} & surfaces) or bool(
        {"support_readiness_check", "marketing_claims_check"} & lanes
    )
    return support_or_adversarial and not _bool(classification.get("runtime_payload_contract_present", False))


def _mechanical_repair_required(conditions: list[str]) -> bool:
    return any(any(marker in condition.lower() for marker in MECHANICAL_MARKERS) for condition in conditions)


def _operator_repair_required(conditions: list[str]) -> bool:
    return any(any(marker in condition.lower() for marker in OPERATOR_MARKERS) for condition in conditions)


def _merge_readiness_disagrees(
    classification: dict[str, object],
    review_receipts: dict[str, object] | None,
    downstream_matrix: dict[str, object] | None,
) -> bool:
    classifier_claims_ready = _bool(classification.get("merge_ready", False))
    if not classifier_claims_ready:
        return False
    if review_receipts is not None and not _bool(review_receipts.get("merge_ready", False)):
        return True
    if downstream_matrix is not None:
        readiness = downstream_matrix.get("readiness", {})
        if isinstance(readiness, dict):
            readiness_data = cast(dict[str, object], readiness)
            if not _bool(readiness_data.get("merge_ready", False)):
                return True
    return False


def _choose_state(
    *,
    classification_ready: bool,
    review_ready: bool,
    merge_ready_dry_run: bool,
    missing_reviews: list[str],
    stale_reviews: list[str],
    invalid_reasons: list[str],
    merge_blocking_conditions: list[str],
    repeated_receipt_loop: bool,
    protected_gate_required: bool,
    support_without_payload: bool,
    mechanical_repair_required: bool,
    operator_required: bool,
) -> tuple[str, str, str, str]:
    if repeated_receipt_loop:
        return ("park", "break_repeated_receipt_loop_before_dispatch", "operator", "break_repeated_receipt_loop_before_dispatch")
    if support_without_payload:
        return ("park", "provide_primary_runtime_payload_before_support_or_adversarial_dispatch", "operator", "primary_payload_present")
    if protected_gate_required:
        return ("awaiting_human_gate", "obtain_human_or_protected_gate_evidence", "human_operator", "protected_gate_receipt_present")
    if stale_reviews:
        return ("awaiting_review_refresh", "refresh_stale_review_receipts_for_current_head", "reviewer", "receipts_match_current_head")
    if _has_request_changes(invalid_reasons):
        return ("awaiting_repair", "repair_requested_changes_or_unresolved_blockers", "codex_or_operator", "blocking_review_verdicts_cleared")
    if "merge_readiness_claim_disagrees_with_artifacts" in merge_blocking_conditions:
        return ("awaiting_repair", "reconcile_claimed_merge_readiness_with_classifier_and_review_artifacts", "codex_or_operator", "readiness_artifacts_agree")
    if operator_required:
        return ("awaiting_operator_repair", "repair_missing_or_invalid_artifacts", "operator", "required_artifacts_present")
    if mechanical_repair_required:
        return ("awaiting_codex_repair", "run_mechanical_repair_for_blockers", "codex", "mechanical_blockers_cleared")
    if "opposite_provider_adversarial" in missing_reviews:
        return ("awaiting_opposite_provider_review", "obtain_opposite_provider_adversarial_review_receipt", "opposite_provider_reviewer", "opposite_provider_receipt_present")
    if "adversarial" in missing_reviews:
        return ("ready_for_adversarial_review", "obtain_adversarial_review_receipt", "adversarial_reviewer", "adversarial_receipt_present")
    if "secondary" in missing_reviews:
        return ("ready_for_secondary_review", "obtain_secondary_review_receipt", "secondary_reviewer", "secondary_receipt_present")
    if missing_reviews:
        return ("awaiting_operator_repair", "obtain_required_review_receipts", "operator", "required_receipts_present")
    if merge_ready_dry_run:
        return ("merge_ready_dry_run", "operator_may_consider_manual_merge_after_live_checks_and_authority", "operator", "operator_confirms_authority_and_live_checks")
    if not classification_ready:
        return ("awaiting_operator_repair", "repair_classifier_or_pr_body_readiness", "operator", "classification_ready")
    if merge_blocking_conditions:
        return ("awaiting_operator_repair", "clear_remaining_merge_blocking_conditions", "operator", "merge_blockers_cleared")
    if not review_ready:
        return ("awaiting_operator_repair", "resolve_review_readiness_gap", "operator", "review_ready")
    return ("supersede", "inspect_unclassified_dispatch_state", "operator", "dispatch_state_classified")


def build_dispatch_advisory(
    classification: dict[str, object],
    review_receipts: dict[str, object] | None,
    downstream_matrix: dict[str, object] | None,
) -> dict[str, object]:
    missing_reviews = _list_value(review_receipts.get("missing_required_review_types", []) if review_receipts else [])
    invalid_reasons = _list_value(review_receipts.get("invalid_receipt_reasons", []) if review_receipts else [])
    stale_reviews = _extract_stale_reviews(invalid_reasons)
    merge_blocking_conditions = _list_value(classification.get("merge_blocking_conditions", []))
    merge_blocking_conditions.extend(_missing_artifact_conditions(review_receipts, downstream_matrix))
    if _merge_readiness_disagrees(classification, review_receipts, downstream_matrix):
        merge_blocking_conditions.append("merge_readiness_claim_disagrees_with_artifacts")

    classification_ready = _bool(classification.get("body_and_classification_ready", False))
    review_ready = _bool(review_receipts.get("review_ready", False) if review_receipts else False)
    merge_ready_dry_run = classification_ready and review_ready and not merge_blocking_conditions
    if downstream_matrix is not None:
        readiness = downstream_matrix.get("readiness", {})
        if isinstance(readiness, dict):
            readiness_data = cast(dict[str, object], readiness)
            merge_ready_dry_run = merge_ready_dry_run and _bool(readiness_data.get("merge_ready", False))

    protected_gate_required = _bool(classification.get("human_gate_required", False)) or any(
        item in {"human_protected", "founder", "security", "finance_sensitive"} for item in missing_reviews
    )
    repeated_loop = _repeated_receipt_loop(review_receipts, invalid_reasons)
    support_without_payload = _support_or_adversarial_without_payload(classification)
    mechanical_required = _mechanical_repair_required(merge_blocking_conditions)
    operator_required = _operator_repair_required(merge_blocking_conditions)

    advisory_state, next_action, owner, stop_condition = _choose_state(
        classification_ready=classification_ready,
        review_ready=review_ready,
        merge_ready_dry_run=merge_ready_dry_run,
        missing_reviews=missing_reviews,
        stale_reviews=stale_reviews,
        invalid_reasons=invalid_reasons,
        merge_blocking_conditions=merge_blocking_conditions,
        repeated_receipt_loop=repeated_loop,
        protected_gate_required=protected_gate_required,
        support_without_payload=support_without_payload,
        mechanical_repair_required=mechanical_required,
        operator_required=operator_required,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "repo": classification.get("repo", "unknown"),
        "pr_number": classification.get("pr_number", "unknown"),
        "head_sha": _sha_from_classification(classification, "head_sha"),
        "base_sha": _sha_from_classification(classification, "base_sha"),
        "classification_ready": classification_ready,
        "review_ready": review_ready,
        "merge_ready_dry_run": merge_ready_dry_run,
        "required_reviews": _list_value(review_receipts.get("required_review_types", []) if review_receipts else classification.get("required_reviews", [])),
        "missing_reviews": missing_reviews,
        "stale_reviews": stale_reviews,
        "merge_blocking_conditions": sorted(set(merge_blocking_conditions)),
        "advisory_state": advisory_state,
        "recommended_next_action": next_action,
        "recommended_owner": owner,
        "stop_condition": stop_condition,
        "repeated_receipt_loop_detected": repeated_loop,
        "mechanical_repair_required": mechanical_required,
        "protected_gate_required": protected_gate_required,
        "codex_required": mechanical_required and not operator_required,
        "operator_required": operator_required or advisory_state in {"awaiting_operator_repair", "park", "awaiting_human_gate"},
        "non_claims": NON_CLAIMS,
    }


def markdown_report(data: dict[str, object]) -> str:
    lines = ["# Hermes Dispatch Advisory", ""]
    for key in (
        "schema_version",
        "repo",
        "pr_number",
        "head_sha",
        "base_sha",
        "classification_ready",
        "review_ready",
        "merge_ready_dry_run",
        "advisory_state",
        "recommended_next_action",
        "recommended_owner",
        "stop_condition",
        "repeated_receipt_loop_detected",
        "mechanical_repair_required",
        "protected_gate_required",
        "codex_required",
        "operator_required",
    ):
        value = data.get(key)
        rendered = str(value).lower() if isinstance(value, bool) else str(value)
        lines.append(f"- {key}: {rendered}")
    for key in ("required_reviews", "missing_reviews", "stale_reviews", "merge_blocking_conditions", "non_claims"):
        values = _list_value(data.get(key, []))
        lines.append(f"- {key}: {', '.join(values) if values else 'none'}")
    lines.append("")
    return "\n".join(lines)


def _load_json(path: str | None) -> dict[str, object] | None:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--classification", required=True)
    parser.add_argument("--review-receipts")
    parser.add_argument("--downstream-matrix")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    classification = _load_json(args.classification)
    if classification is None:
        raise SystemExit("classification is required")
    review_receipts = _load_json(args.review_receipts)
    downstream_matrix = _load_json(args.downstream_matrix)
    result = build_dispatch_advisory(classification, review_receipts, downstream_matrix)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "hermes-dispatch-advisory.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / "hermes-dispatch-advisory.md").write_text(markdown_report(result), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
