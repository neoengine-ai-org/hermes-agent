from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "review_receipt_validator.py"
spec = importlib.util.spec_from_file_location("review_receipt_validator_protected", MODULE_PATH)
assert spec is not None
validator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = validator
spec.loader.exec_module(validator)

HEAD_SHA = "head-current"
BASE_SHA = "base-current"
FINGERPRINT = "sha256:" + ("a" * 64)


def _classification(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "schema_version": "ci-classification.v1",
        "repo": "neoengine-ai-org/hermes-agent",
        "pr_number": "80",
        "risk_class": "R4",
        "complexity_class": "C4",
        "required_reviews": [],
        "body_and_classification_ready": True,
        "merge_blocking_conditions": [],
    }
    data.update(overrides)
    return data


def _receipt(review_type: str, *, verdict: str = "PASS") -> dict[str, object]:
    return {
        "review_type": review_type,
        "provider": "human" if review_type in {"founder", "human_protected", "tier4_authority_waiver", "tier4_break_glass"} else "reviewer",
        "model": "n/a",
        "provider_family": "human",
        "reviewer_family": review_type,
        "primary_builder_family": "codex",
        "family_relation": "not_applicable",
        "reviewer_identity": f"{review_type}-reviewer",
        "same_provider_fallback": "no",
        "fallback_reason": "",
        "pr_reviewed": "80",
        "head_sha_reviewed": "stale-head",
        "base_sha_reviewed": BASE_SHA,
        "verdict": verdict,
        "material_findings": [],
        "unresolved_blockers": [],
        "protected_claims_checked": [],
        "review_timestamp": "2026-07-28T00:00:00Z",
        "evidence_url_or_path": "https://example.invalid/protected-review",
        "diff_fingerprint": FINGERPRINT,
    }


@pytest.mark.parametrize(
    "review_type",
    [
        "founder",
        "human_protected",
        "tier4_authority_waiver",
        "tier4_break_glass",
        "security",
        "finance_sensitive",
    ],
)
def test_every_protected_receipt_type_rejects_mechanical_freshness(review_type: str) -> None:
    assert validator._mechanically_fresh(_receipt(review_type), FINGERPRINT) is False


@pytest.mark.parametrize(
    ("review_type", "required_marker", "flag"),
    [
        ("founder", "founder_review_required", "founder_review_required"),
        ("human_protected", "protected_human_review_required", "human_gate_required"),
        ("security", "security_review_required", "unused"),
        ("finance_sensitive", "finance_sensitive_review_required", "unused"),
    ],
)
def test_required_protected_receipt_stays_stale_on_matching_fingerprint(
    review_type: str,
    required_marker: str,
    flag: str,
) -> None:
    classification = _classification(required_reviews=[required_marker])
    if flag != "unused":
        classification[flag] = True

    result = validator.validate_review_receipts(
        classification,
        [_receipt(review_type)],
        current_head_sha=HEAD_SHA,
        current_base_sha=BASE_SHA,
        current_diff_fingerprint=FINGERPRINT,
        base_is_ancestor_of_head=True,
    )

    assert result["review_ready"] is False
    assert f"stale_review_receipt:{review_type}" in result["invalid_receipt_reasons"]
    assert review_type not in result["mechanical_freshness_receipts"]


@pytest.mark.parametrize(
    ("review_type", "verdict"),
    [
        ("tier4_authority_waiver", "WAIVED_BY_AUTHORITY"),
        ("tier4_break_glass", "BREAK_GLASS"),
    ],
)
def test_stale_tier4_override_cannot_satisfy_current_head(
    review_type: str,
    verdict: str,
) -> None:
    result = validator.validate_review_receipts(
        _classification(
            model_tier_required=4,
            opposite_frontier_required=True,
            required_reviews=["opposite_frontier_cc_review_required"],
        ),
        [_receipt(review_type, verdict=verdict)],
        current_head_sha=HEAD_SHA,
        current_base_sha=BASE_SHA,
        current_diff_fingerprint=FINGERPRINT,
        base_is_ancestor_of_head=True,
    )

    assert result["review_ready"] is False
    assert result["merge_ready"] is False
    assert f"stale_review_receipt:{review_type}" in result["invalid_receipt_reasons"]
    assert "opposite_frontier" in result["missing_required_review_types"]
