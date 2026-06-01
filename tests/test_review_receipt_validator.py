from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "review_receipt_validator.py"
spec = importlib.util.spec_from_file_location("review_receipt_validator", MODULE_PATH)
assert spec is not None
review_receipt_validator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = review_receipt_validator
spec.loader.exec_module(review_receipt_validator)


HEAD_SHA = "head123"
BASE_SHA = "base123"


def classification(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "schema_version": "ci-classification.v1",
        "repo": "neoengine-ai-org/hermes-agent",
        "pr_number": "9",
        "risk_class": "R1",
        "complexity_class": "C1",
        "required_reviews": ["no_secondary_review_required"],
        "secondary_review_required": False,
        "adversarial_review_required": False,
        "opposite_provider_required": False,
        "human_gate_required": False,
        "founder_review_required": False,
        "body_and_classification_ready": True,
        "merge_blocking_conditions": [],
    }
    data.update(overrides)
    return data


def receipt(review_type: str, **overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "review_type": review_type,
        "provider": "codex",
        "reviewer_identity": f"{review_type}-reviewer",
        "same_provider_fallback": "no",
        "fallback_reason": "",
        "pr_reviewed": "9",
        "head_sha_reviewed": HEAD_SHA,
        "base_sha_reviewed": BASE_SHA,
        "verdict": "PASS",
        "material_findings": [],
        "unresolved_blockers": [],
        "protected_claims_checked": [],
        "review_timestamp": "2026-06-01T00:00:00Z",
        "evidence_url_or_path": "https://example.invalid/review",
    }
    data.update(overrides)
    return data


def validate(data: dict[str, object], receipts: list[dict[str, object]]) -> dict[str, Any]:
    return review_receipt_validator.validate_review_receipts(
        data,
        receipts,
        current_head_sha=HEAD_SHA,
        current_base_sha=BASE_SHA,
    )


def test_no_reviews_required_marks_review_ready() -> None:
    result = validate(classification(), [])

    assert result["review_ready"] is True
    assert result["merge_ready"] is True
    assert result["missing_required_review_types"] == []
    assert result["evidence_verification_mode"] == "self_attested_pr_body_or_json"
    assert "not_identity_verified" in result["non_claims"]
    assert "not_provider_authenticated" in result["non_claims"]


def test_default_pr_template_placeholder_receipt_is_ignored() -> None:
    template = (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")

    receipts = review_receipt_validator.parse_review_receipts_from_markdown(template)
    result = validate(classification(), receipts)

    assert receipts == []
    assert result["review_ready"] is True


def test_secondary_review_required_but_missing_marks_not_ready() -> None:
    result = validate(
        classification(required_reviews=["secondary_review_required"], secondary_review_required=True),
        [],
    )

    assert result["review_ready"] is False
    assert "secondary" in result["missing_required_review_types"]


def test_adversarial_review_required_but_missing_marks_not_ready() -> None:
    result = validate(
        classification(required_reviews=["adversarial_review_required"], adversarial_review_required=True),
        [],
    )

    assert result["review_ready"] is False
    assert "adversarial" in result["missing_required_review_types"]


def test_opposite_provider_same_provider_fallback_without_reason_marks_not_ready() -> None:
    result = validate(
        classification(required_reviews=["opposite_provider_adversarial_required"], opposite_provider_required=True),
        [receipt("opposite_provider_adversarial", same_provider_fallback="yes", fallback_reason="")],
    )

    assert result["review_ready"] is False
    assert "same_provider_fallback_without_reason:opposite_provider_adversarial" in result["invalid_receipt_reasons"]


def test_same_provider_fallback_with_reason_is_allowed_for_r3() -> None:
    result = validate(
        classification(risk_class="R3", required_reviews=["opposite_provider_adversarial_required"], opposite_provider_required=True),
        [receipt("opposite_provider_adversarial", same_provider_fallback="yes", fallback_reason="opposite provider unavailable in dry-run")],
    )

    assert result["review_ready"] is True
    assert result["merge_ready"] is True


def test_opposite_provider_receipt_from_primary_provider_without_fallback_marks_not_ready() -> None:
    result = review_receipt_validator.validate_review_receipts(
        classification(risk_class="R3", required_reviews=["opposite_provider_adversarial_required"], opposite_provider_required=True),
        [receipt("opposite_provider_adversarial", provider="codex", same_provider_fallback="no")],
        current_head_sha=HEAD_SHA,
        current_base_sha=BASE_SHA,
        primary_provider="codex",
    )

    assert result["review_ready"] is False
    assert "same_provider_without_fallback:opposite_provider_adversarial" in result["invalid_receipt_reasons"]


def test_same_provider_fallback_with_reason_is_not_allowed_for_r4() -> None:
    result = validate(
        classification(risk_class="R4", required_reviews=["opposite_provider_adversarial_required"], opposite_provider_required=True),
        [receipt("opposite_provider_adversarial", same_provider_fallback="yes", fallback_reason="opposite provider unavailable in dry-run")],
    )

    assert result["review_ready"] is False
    assert "same_provider_fallback_not_allowed_for_risk:R4:opposite_provider_adversarial" in result["invalid_receipt_reasons"]


def test_stale_head_sha_marks_not_ready() -> None:
    result = validate(
        classification(required_reviews=["secondary_review_required"], secondary_review_required=True),
        [receipt("secondary", head_sha_reviewed="oldhead")],
    )

    assert result["review_ready"] is False
    assert "stale_review_receipt:secondary" in result["invalid_receipt_reasons"]


def test_pr_number_mismatch_marks_not_ready() -> None:
    result = validate(
        classification(required_reviews=["secondary_review_required"], secondary_review_required=True),
        [receipt("secondary", pr_reviewed="8")],
    )

    assert result["review_ready"] is False
    assert "pr_number_mismatch:secondary" in result["invalid_receipt_reasons"]


def test_request_changes_marks_not_ready() -> None:
    result = validate(
        classification(required_reviews=["secondary_review_required"], secondary_review_required=True),
        [receipt("secondary", verdict="REQUEST_CHANGES")],
    )

    assert result["review_ready"] is False
    assert "blocking_verdict:secondary:REQUEST_CHANGES" in result["invalid_receipt_reasons"]


def test_unresolved_blockers_mark_not_ready() -> None:
    result = validate(
        classification(required_reviews=["secondary_review_required"], secondary_review_required=True),
        [receipt("secondary", unresolved_blockers=["fix failing tests"])],
    )

    assert result["review_ready"] is False
    assert "unresolved_blockers:secondary" in result["invalid_receipt_reasons"]


def test_all_required_receipts_present_current_and_pass_marks_review_ready() -> None:
    result = validate(
        classification(
            risk_class="R3",
            complexity_class="C3",
            required_reviews=[
                "secondary_review_required",
                "adversarial_review_required",
                "opposite_provider_adversarial_required",
            ],
            secondary_review_required=True,
            adversarial_review_required=True,
            opposite_provider_required=True,
        ),
        [
            receipt("secondary"),
            receipt("adversarial"),
            receipt("opposite_provider_adversarial", provider="claude"),
        ],
    )

    assert result["review_ready"] is True
    assert result["merge_ready"] is True
    assert result["satisfied_required_review_types"] == ["adversarial", "opposite_provider_adversarial", "secondary"]


def test_human_protected_review_required_but_missing_reports_dry_run_missing() -> None:
    result = validate(
        classification(required_reviews=["protected_human_review_required"], human_gate_required=True),
        [],
    )

    assert result["review_ready"] is False
    assert "human_protected" in result["missing_required_review_types"]
    assert result["enforced"] is False


def test_security_and_finance_receipts_are_required_from_required_reviews() -> None:
    result = validate(
        classification(required_reviews=["security_review_required", "finance_sensitive_review_required"]),
        [receipt("security"), receipt("finance_sensitive")],
    )

    assert result["review_ready"] is True
    assert result["required_review_types"] == ["finance_sensitive", "security"]


def test_parse_review_receipts_from_markdown() -> None:
    markdown = f"""
## Review Receipt: secondary

- review_type: secondary
- provider: codex
- reviewer_identity: reviewer-1
- same_provider_fallback: no
- fallback_reason:
- pr_reviewed: 9
- head_sha_reviewed: {HEAD_SHA}
- base_sha_reviewed: {BASE_SHA}
- verdict: PASS
- material_findings: none
- unresolved_blockers: none
- protected_claims_checked: none
- review_timestamp: 2026-06-01T00:00:00Z
- evidence_url_or_path: https://example.invalid/review
"""

    receipts = review_receipt_validator.parse_review_receipts_from_markdown(markdown)

    assert receipts == [receipt("secondary", reviewer_identity="reviewer-1", material_findings=[], protected_claims_checked=[])]


def test_cli_emits_artifacts_even_when_review_ready_false(tmp_path: Path) -> None:
    classification_path = tmp_path / "ci-classification.json"
    classification_path.write_text(
        json.dumps(classification(required_reviews=["secondary_review_required"], secondary_review_required=True)),
        encoding="utf-8",
    )
    body_path = tmp_path / "body.md"
    body_path.write_text("No review receipts here\n", encoding="utf-8")
    output_dir = tmp_path / "out"

    completed = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--classification",
            str(classification_path),
            "--pr-body",
            str(body_path),
            "--head-sha",
            HEAD_SHA,
            "--base-sha",
            BASE_SHA,
            "--output-dir",
            str(output_dir),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    data = json.loads((output_dir / "review-receipts.json").read_text(encoding="utf-8"))
    assert data["review_ready"] is False
    assert (output_dir / "review-receipts.md").exists()
