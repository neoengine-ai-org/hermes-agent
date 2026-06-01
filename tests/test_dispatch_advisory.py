from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "dispatch_advisory.py"
spec = importlib.util.spec_from_file_location("dispatch_advisory", MODULE_PATH)
assert spec is not None
dispatch_advisory = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = dispatch_advisory
spec.loader.exec_module(dispatch_advisory)


HEAD_SHA = "head10"
BASE_SHA = "base10"


def classification(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "schema_version": "ci-classification.v1",
        "repo": "neoengine-ai-org/hermes-agent",
        "pr_number": "10",
        "classifier_execution": {"head_sha": HEAD_SHA, "base_sha": BASE_SHA},
        "head_sha": HEAD_SHA,
        "base_sha": BASE_SHA,
        "body_and_classification_ready": True,
        "review_ready": False,
        "merge_ready": False,
        "required_reviews": [],
        "merge_blocking_conditions": [],
        "runtime_payload_contract_present": True,
        "impacted_surfaces": ["ci_workflow"],
        "required_ci_lanes": [],
        "protected_flags": [],
    }
    data.update(overrides)
    return data


def receipts(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "schema_version": "review-receipts-dry-run.v1",
        "review_ready": False,
        "merge_ready": False,
        "required_review_types": [],
        "missing_required_review_types": [],
        "invalid_receipt_reasons": [],
        "receipts": [],
        "non_claims": ["dry_run_only", "not_identity_verified"],
    }
    data.update(overrides)
    return data


def matrix(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "schema_version": "ci-downstream-matrix-dry-run.v1",
        "readiness": {
            "body_and_classification_ready": True,
            "review_ready": False,
            "merge_ready": False,
        },
        "include": [],
        "non_claims": ["dry_run_only", "not_downstream_ci_matrix_enforced"],
    }
    data.update(overrides)
    return data


def advise(
    cls: dict[str, object],
    rcts: dict[str, object] | None = None,
    mat: dict[str, object] | None = None,
) -> dict[str, Any]:
    return dispatch_advisory.build_dispatch_advisory(cls, rcts, mat)


def test_classification_ready_with_secondary_review_missing_recommends_secondary_review() -> None:
    result = advise(
        classification(required_reviews=["secondary_review_required"]),
        receipts(required_review_types=["secondary"], missing_required_review_types=["secondary"]),
        matrix(),
    )

    assert result["advisory_state"] == "ready_for_secondary_review"
    assert result["recommended_owner"] == "secondary_reviewer"
    assert result["classification_ready"] is True
    assert result["review_ready"] is False
    assert result["merge_ready_dry_run"] is False
    assert "secondary" in result["missing_reviews"]


def test_adversarial_review_missing_recommends_adversarial_review() -> None:
    result = advise(
        classification(required_reviews=["adversarial_review_required"]),
        receipts(required_review_types=["adversarial"], missing_required_review_types=["adversarial"]),
        matrix(),
    )

    assert result["advisory_state"] == "ready_for_adversarial_review"
    assert result["recommended_owner"] == "adversarial_reviewer"


def test_opposite_provider_required_missing_recommends_opposite_provider_review() -> None:
    result = advise(
        classification(required_reviews=["opposite_provider_adversarial_required"]),
        receipts(required_review_types=["opposite_provider_adversarial"], missing_required_review_types=["opposite_provider_adversarial"]),
        matrix(),
    )

    assert result["advisory_state"] == "awaiting_opposite_provider_review"
    assert result["recommended_owner"] == "opposite_provider_reviewer"


def test_protected_human_gate_required_recommends_human_gate() -> None:
    result = advise(
        classification(human_gate_required=True, protected_flags=["human_gate_required"]),
        receipts(required_review_types=["human_protected"], missing_required_review_types=["human_protected"]),
        matrix(),
    )

    assert result["advisory_state"] == "awaiting_human_gate"
    assert result["protected_gate_required"] is True
    assert result["recommended_owner"] == "human_operator"


def test_mechanical_blocker_recommends_codex_repair() -> None:
    result = advise(
        classification(merge_blocking_conditions=["ruff_failed", "py_compile_failed"]),
        receipts(review_ready=True),
        matrix(),
    )

    assert result["advisory_state"] == "awaiting_codex_repair"
    assert result["mechanical_repair_required"] is True
    assert result["codex_required"] is True
    assert result["operator_required"] is False


def test_operator_artifact_blocker_recommends_operator_repair() -> None:
    result = advise(classification(), None, matrix())

    assert result["advisory_state"] == "awaiting_operator_repair"
    assert result["operator_required"] is True
    assert "artifact_missing:review-receipts.json" in result["merge_blocking_conditions"]


def test_repeated_receipt_loop_recommends_park() -> None:
    result = advise(
        classification(required_reviews=["secondary_review_required"]),
        receipts(
            required_review_types=["secondary"],
            missing_required_review_types=["secondary"],
            invalid_receipt_reasons=["stale_review_receipt:secondary", "blocking_verdict:secondary:REQUEST_CHANGES"],
            receipts=[{"review_type": "secondary"}, {"review_type": "secondary"}],
        ),
        matrix(),
    )

    assert result["repeated_receipt_loop_detected"] is True
    assert result["advisory_state"] == "park"
    assert result["stop_condition"] == "break_repeated_receipt_loop_before_dispatch"


def test_support_lane_without_primary_payload_recommends_park() -> None:
    result = advise(
        classification(
            impacted_surfaces=["support_customer_ops"],
            required_ci_lanes=["support_readiness_check"],
            runtime_payload_contract_present=False,
        ),
        receipts(review_ready=True),
        matrix(),
    )

    assert result["advisory_state"] == "park"
    assert result["recommended_next_action"] == "provide_primary_runtime_payload_before_support_or_adversarial_dispatch"


def test_all_dry_run_ready_recommends_merge_ready_dry_run_without_authority_claim() -> None:
    result = advise(
        classification(body_and_classification_ready=True, review_ready=True, merge_ready=True),
        receipts(review_ready=True, merge_ready=True),
        matrix(readiness={"body_and_classification_ready": True, "review_ready": True, "merge_ready": True}),
    )

    assert result["advisory_state"] == "merge_ready_dry_run"
    assert result["merge_ready_dry_run"] is True
    assert result["recommended_owner"] == "operator"
    assert "not_auto_merge_authority" in result["non_claims"]
    assert "not_dispatcher_enforced" in result["non_claims"]


def test_stale_review_receipt_recommends_review_refresh() -> None:
    result = advise(
        classification(required_reviews=["secondary_review_required"]),
        receipts(invalid_receipt_reasons=["stale_review_receipt:secondary"], missing_required_review_types=["secondary"]),
        matrix(),
    )

    assert result["advisory_state"] == "awaiting_review_refresh"
    assert result["stale_reviews"] == ["secondary"]


def test_request_changes_receipt_recommends_repair() -> None:
    result = advise(
        classification(required_reviews=["secondary_review_required"]),
        receipts(invalid_receipt_reasons=["blocking_verdict:secondary:REQUEST_CHANGES"]),
        matrix(),
    )

    assert result["advisory_state"] == "awaiting_repair"
    assert result["recommended_owner"] == "codex_or_operator"


def test_pr_claims_merge_readiness_but_artifacts_disagree_recommends_repair() -> None:
    result = advise(
        classification(body_and_classification_ready=True, review_ready=True, merge_ready=True),
        receipts(review_ready=False, merge_ready=False, missing_required_review_types=["secondary"]),
        matrix(readiness={"body_and_classification_ready": True, "review_ready": False, "merge_ready": False}),
    )

    assert result["advisory_state"] == "awaiting_repair"
    assert "merge_readiness_claim_disagrees_with_artifacts" in result["merge_blocking_conditions"]


def test_cli_emits_json_and_markdown_artifacts(tmp_path: Path) -> None:
    classification_path = tmp_path / "ci-classification.json"
    receipts_path = tmp_path / "review-receipts.json"
    matrix_path = tmp_path / "downstream-matrix.json"
    out_dir = tmp_path / "out"
    classification_path.write_text(json.dumps(classification(required_reviews=["secondary_review_required"])), encoding="utf-8")
    receipts_path.write_text(json.dumps(receipts(missing_required_review_types=["secondary"])), encoding="utf-8")
    matrix_path.write_text(json.dumps(matrix()), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--classification",
            str(classification_path),
            "--review-receipts",
            str(receipts_path),
            "--downstream-matrix",
            str(matrix_path),
            "--output-dir",
            str(out_dir),
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    emitted = json.loads((out_dir / "hermes-dispatch-advisory.json").read_text(encoding="utf-8"))
    assert json.loads(completed.stdout)["schema_version"] == "hermes-dispatch-advisory.v1"
    assert emitted["advisory_state"] == "ready_for_secondary_review"
    assert (out_dir / "hermes-dispatch-advisory.md").read_text(encoding="utf-8").startswith("# Hermes Dispatch Advisory")
