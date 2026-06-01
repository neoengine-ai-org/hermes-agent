#!/usr/bin/env python3
"""Validate required review receipts in advisory/dry-run mode.

Consumes the trusted PR risk classifier artifact and review receipts from PR-body
Markdown and emits review-receipts.json / review-receipts.md. This script never
exits non-zero for missing review evidence; PR #9 is dry-run reporting only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

SCHEMA_VERSION = "review-receipts-dry-run.v1"
RECEIPT_TYPES = {
    "secondary",
    "adversarial",
    "opposite_provider_adversarial",
    "human_protected",
    "founder",
    "security",
    "finance_sensitive",
}
PASSING_VERDICTS = {"PASS", "PASS_WITH_CAVEATS"}
BLOCKING_VERDICTS = {
    "REQUEST_CHANGES",
    "PARK",
    "SUPERSEDE",
    "PROTECTED_GATE_REQUIRED",
    "MERGE_REPAIR_REQUIRED",
}
RECEIPT_FIELDS = [
    "review_type",
    "provider",
    "reviewer_identity",
    "same_provider_fallback",
    "fallback_reason",
    "pr_reviewed",
    "head_sha_reviewed",
    "base_sha_reviewed",
    "verdict",
    "material_findings",
    "unresolved_blockers",
    "protected_claims_checked",
    "review_timestamp",
    "evidence_url_or_path",
]
RISK_ORDER = ["R0", "R1", "R2", "R3", "R4", "R5"]


def _list_value(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value is None:
        return []
    text = str(value).strip()
    if not text or text.lower() in {"none", "n/a", "na"}:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"yes", "true", "1"}


def _required_review_types(classification: dict[str, object]) -> list[str]:
    required = set()
    required_reviews = set(_list_value(classification.get("required_reviews", [])))
    if classification.get("secondary_review_required") or "secondary_review_required" in required_reviews:
        required.add("secondary")
    if classification.get("adversarial_review_required") or "adversarial_review_required" in required_reviews:
        required.add("adversarial")
    if classification.get("opposite_provider_required") or "opposite_provider_adversarial_required" in required_reviews:
        required.add("opposite_provider_adversarial")
    if classification.get("human_gate_required") or "protected_human_review_required" in required_reviews:
        required.add("human_protected")
    if classification.get("founder_review_required") or "founder_review_required" in required_reviews:
        required.add("founder")
    if "security_review_required" in required_reviews:
        required.add("security")
    if "finance_sensitive_review_required" in required_reviews:
        required.add("finance_sensitive")
    return sorted(required)


def _fallback_allowed_for_risk(risk_class: str) -> bool:
    if risk_class not in RISK_ORDER:
        return False
    return RISK_ORDER.index(risk_class) <= RISK_ORDER.index("R3")


def _normalize_receipt(receipt: dict[str, object]) -> dict[str, object]:
    normalized = {field: receipt.get(field, "") for field in RECEIPT_FIELDS}
    normalized["review_type"] = str(normalized["review_type"]).strip()
    normalized["provider"] = str(normalized["provider"]).strip()
    normalized["reviewer_identity"] = str(normalized["reviewer_identity"]).strip()
    normalized["same_provider_fallback"] = "yes" if _bool_value(normalized["same_provider_fallback"]) else "no"
    normalized["fallback_reason"] = str(normalized["fallback_reason"]).strip()
    normalized["pr_reviewed"] = str(normalized["pr_reviewed"]).strip()
    normalized["head_sha_reviewed"] = str(normalized["head_sha_reviewed"]).strip()
    normalized["base_sha_reviewed"] = str(normalized["base_sha_reviewed"]).strip()
    normalized["verdict"] = str(normalized["verdict"]).strip().upper()
    normalized["material_findings"] = _list_value(normalized["material_findings"])
    normalized["unresolved_blockers"] = _list_value(normalized["unresolved_blockers"])
    normalized["protected_claims_checked"] = _list_value(normalized["protected_claims_checked"])
    normalized["review_timestamp"] = str(normalized["review_timestamp"]).strip()
    normalized["evidence_url_or_path"] = str(normalized["evidence_url_or_path"]).strip()
    return normalized


def _is_template_placeholder_receipt(receipt: dict[str, object]) -> bool:
    review_type = str(receipt.get("review_type", "")).strip()
    return not review_type or "/" in review_type or review_type.startswith("<")


def _append_normalized_receipt(receipts: list[dict[str, object]], current: dict[str, object]) -> None:
    normalized = _normalize_receipt(current)
    if _is_template_placeholder_receipt(normalized):
        return
    receipts.append(normalized)


def parse_review_receipts_from_markdown(markdown: str) -> list[dict[str, object]]:
    receipts: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("## review receipt"):
            if current:
                _append_normalized_receipt(receipts, current)
            current = {}
            continue
        if current is None:
            continue
        if stripped.startswith("## "):
            if current:
                _append_normalized_receipt(receipts, current)
            current = None
            continue
        if stripped.startswith("- ") and ":" in stripped:
            key, value = stripped[2:].split(":", 1)
            current[key.strip()] = value.strip()
    if current:
        _append_normalized_receipt(receipts, current)
    return receipts


def _receipt_invalid_reasons(
    receipt: dict[str, object],
    *,
    current_head_sha: str,
    current_base_sha: str,
    risk_class: str,
    pr_number: str,
    primary_provider: str = "",
) -> list[str]:
    review_type = str(receipt.get("review_type", ""))
    reasons: list[str] = []
    if review_type not in RECEIPT_TYPES:
        reasons.append(f"invalid_review_type:{review_type or 'missing'}")
    for field in ("provider", "reviewer_identity", "pr_reviewed", "head_sha_reviewed", "base_sha_reviewed", "verdict", "review_timestamp", "evidence_url_or_path"):
        if not receipt.get(field):
            reasons.append(f"missing_receipt_field:{review_type}:{field}")
    if receipt.get("head_sha_reviewed") != current_head_sha:
        reasons.append(f"stale_review_receipt:{review_type}")
    if current_base_sha != "unknown" and receipt.get("base_sha_reviewed") not in {current_base_sha, "unknown"}:
        reasons.append(f"base_sha_mismatch:{review_type}")
    if pr_number != "unknown" and str(receipt.get("pr_reviewed", "")).strip() != pr_number:
        reasons.append(f"pr_number_mismatch:{review_type}")
    verdict = str(receipt.get("verdict", ""))
    if verdict in BLOCKING_VERDICTS or verdict not in PASSING_VERDICTS:
        reasons.append(f"blocking_verdict:{review_type}:{verdict or 'missing'}")
    if _list_value(receipt.get("unresolved_blockers", [])):
        reasons.append(f"unresolved_blockers:{review_type}")
    if receipt.get("same_provider_fallback") == "yes":
        if not str(receipt.get("fallback_reason", "")).strip():
            reasons.append(f"same_provider_fallback_without_reason:{review_type}")
        if not _fallback_allowed_for_risk(risk_class):
            reasons.append(f"same_provider_fallback_not_allowed_for_risk:{risk_class}:{review_type}")
    elif review_type == "opposite_provider_adversarial" and primary_provider:
        if str(receipt.get("provider", "")).strip().lower() == primary_provider.strip().lower():
            reasons.append(f"same_provider_without_fallback:{review_type}")
    return reasons


def validate_review_receipts(
    classification: dict[str, object],
    receipts: list[dict[str, object]],
    *,
    current_head_sha: str,
    current_base_sha: str = "unknown",
    primary_provider: str = "",
) -> dict[str, object]:
    normalized_receipts = [_normalize_receipt(receipt) for receipt in receipts]
    required = _required_review_types(classification)
    risk_class = str(classification.get("risk_class", "R0"))
    invalid_reasons: list[str] = []
    valid_by_type: dict[str, list[dict[str, object]]] = {review_type: [] for review_type in RECEIPT_TYPES}

    for receipt in normalized_receipts:
        reasons = _receipt_invalid_reasons(
            receipt,
            current_head_sha=current_head_sha,
            current_base_sha=current_base_sha,
            risk_class=risk_class,
            pr_number=str(classification.get("pr_number", "unknown")),
            primary_provider=primary_provider,
        )
        if reasons:
            invalid_reasons.extend(reasons)
        else:
            review_type = str(receipt["review_type"])
            valid_by_type.setdefault(review_type, []).append(receipt)

    missing = sorted(review_type for review_type in required if not valid_by_type.get(review_type))
    satisfied = sorted(review_type for review_type in required if valid_by_type.get(review_type))
    review_ready = not missing and not invalid_reasons
    body_ready = bool(classification.get("body_and_classification_ready", False))
    merge_blockers = _list_value(classification.get("merge_blocking_conditions", []))
    merge_ready = body_ready and review_ready and not merge_blockers

    non_claims = [
        "dry_run_only",
        "not_dispatcher_enforced",
        "not_downstream_matrix_enforced",
        "not_cross_repo_propagated",
        "not_protected_approval",
        "not_identity_verified",
        "not_provider_authenticated",
        "not_evidence_url_verified",
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "repo": classification.get("repo", "unknown"),
        "pr_number": classification.get("pr_number", "unknown"),
        "risk_class": risk_class,
        "complexity_class": classification.get("complexity_class", "unknown"),
        "enforced": False,
        "evidence_verification_mode": "self_attested_pr_body_or_json",
        "non_claims": non_claims,
        "current_head_sha": current_head_sha,
        "current_base_sha": current_base_sha,
        "primary_provider": primary_provider or "unknown",
        "required_review_types": required,
        "satisfied_required_review_types": satisfied,
        "missing_required_review_types": missing,
        "invalid_receipt_reasons": sorted(set(invalid_reasons)),
        "receipts": normalized_receipts,
        "body_and_classification_ready": body_ready,
        "review_ready": review_ready,
        "merge_ready": merge_ready,
    }


def markdown_report(data: dict[str, object]) -> str:
    lines = ["# Review Receipt Validation", ""]
    for key in (
        "schema_version",
        "repo",
        "pr_number",
        "risk_class",
        "complexity_class",
        "enforced",
        "evidence_verification_mode",
        "body_and_classification_ready",
        "review_ready",
        "merge_ready",
    ):
        value = data.get(key)
        rendered = str(value).lower() if isinstance(value, bool) else str(value)
        lines.append(f"- {key}: {rendered}")
    for key in ("required_review_types", "satisfied_required_review_types", "missing_required_review_types", "invalid_receipt_reasons", "non_claims"):
        values = _list_value(data.get(key, []))
        lines.append(f"- {key}: {', '.join(values) if values else 'none'}")
    lines.append("")
    lines.append("## Receipts")
    receipts = data.get("receipts", [])
    if isinstance(receipts, list) and receipts:
        for receipt in receipts:
            if isinstance(receipt, dict):
                receipt_data = cast(dict[str, object], receipt)
                lines.append(
                    f"- {receipt_data.get('review_type', 'unknown')}: {receipt_data.get('verdict', 'unknown')} "
                    f"by {receipt_data.get('reviewer_identity', 'unknown')} ({receipt_data.get('provider', 'unknown')})"
                )
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--classification", required=True)
    parser.add_argument("--pr-body")
    parser.add_argument("--receipts-json")
    parser.add_argument("--head-sha", default="unknown")
    parser.add_argument("--base-sha", default="unknown")
    parser.add_argument("--primary-provider", default="codex")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    classification = json.loads(Path(args.classification).read_text(encoding="utf-8"))
    receipts: list[dict[str, object]] = []
    if args.pr_body:
        receipts.extend(parse_review_receipts_from_markdown(Path(args.pr_body).read_text(encoding="utf-8")))
    if args.receipts_json:
        loaded = json.loads(Path(args.receipts_json).read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            receipts.extend(item for item in loaded if isinstance(item, dict))
        elif isinstance(loaded, dict) and isinstance(loaded.get("receipts"), list):
            receipts.extend(item for item in loaded["receipts"] if isinstance(item, dict))

    result = validate_review_receipts(
        classification,
        receipts,
        current_head_sha=args.head_sha,
        current_base_sha=args.base_sha,
        primary_provider=args.primary_provider,
    )
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "review-receipts.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / "review-receipts.md").write_text(markdown_report(result), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
