from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "ci_risk_classifier.py"
spec = importlib.util.spec_from_file_location("ci_risk_classifier", MODULE_PATH)
assert spec is not None
ci_risk_classifier = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = ci_risk_classifier
spec.loader.exec_module(ci_risk_classifier)


def classification(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "risk_class": "R3",
        "complexity_class": "C3",
        "required_ci_lanes": ["pr_body_contract", "diff_check", "workflow_lint"],
        "required_reviews": ["secondary_review_required", "adversarial_review_required", "opposite_provider_adversarial_required"],
        "protected_flags": ["secrets_tokens"],
        "merge_blocking_conditions": ["runtime_surface_without_runtimePayloadContract"],
        "allowed_to_mark_ready": False,
        "secondary_review_required": True,
        "adversarial_review_required": True,
        "opposite_provider_required": True,
        "human_gate_required": False,
        "founder_review_required": False,
    }
    data.update(overrides)
    return data


def body(**overrides: str) -> str:
    fields = {
        "Risk class": "R3",
        "Complexity class": "C3",
        "Impacted surfaces": "ci_workflow",
        "RuntimePayloadContract present": "yes",
        "Blocker exemption, if any": "N/A",
        "Secondary review required": "yes",
        "Adversarial review required": "yes",
        "Opposite-provider adversarial required": "yes",
        "Human/protected review required": "no",
        "Founder review required": "no",
        "Required CI lanes": "pr_body_contract, diff_check, docs_impact, typecheck, targeted_runtime_tests, backend_runtime, build, governance_required, workflow_lint, pr_impact_classifier_tests",
        "Skipped CI lanes and rationale": "protected launch lanes skipped; not a launch change",
        "Token class": "L",
        "Expected state change": "trusted classifier execution metadata and fail-closed comparison",
        "Stop condition": "classification artifacts emitted",
    }
    fields.update(overrides)
    lines = ["## Risk, Complexity, Review, and CI Classification", ""]
    lines.extend(f"- {key}: {value}" for key, value in fields.items())
    lines.extend(
        [
            "",
            "## RuntimePayloadContract",
            "",
            "- user_or_operator_visible_outcome: PRs are classified by trusted base code.",
            "- runtime_surface_touched: PR risk classifier workflow and script.",
            "- product_or_platform_capability_advanced: Spoof-resistant CI governance foundation.",
            "- why_this_is_not_only_docs_or_scaffolding: The workflow executes classifier code and fails closed.",
            "- tests_that_prove_runtime_behavior: tests/test_trusted_ci_risk_classifier.py",
            "- acceptance_gate: Classifier tests and live workflow pass.",
            "- rollback: Revert trusted classifier workflow changes.",
            "- protected_non_claims: No org-wide gate, protected approval, launch, or customer-data readiness claimed.",
        ]
    )
    return "\n".join(lines) + "\n"


def test_classifier_self_change_detects_script_workflow_and_template() -> None:
    files = [
        "scripts/ci_risk_classifier.py",
        ".github/workflows/pr-risk-classifier.yml",
        ".github/PULL_REQUEST_TEMPLATE.md",
    ]

    assert ci_risk_classifier.classifier_self_change(files) is True

    result = ci_risk_classifier.classify(files, body(), additions=40)
    assert "ci_workflow" in result.impacted_surfaces
    assert result.risk_class == "R3"
    assert result.adversarial_review_required is True


def test_head_classifier_weakenings_detect_required_posture_loss() -> None:
    base = classification()
    head = classification(
        risk_class="R2",
        complexity_class="C2",
        required_ci_lanes=["pr_body_contract", "diff_check"],
        required_reviews=["secondary_review_required"],
        protected_flags=[],
        merge_blocking_conditions=[],
        allowed_to_mark_ready=True,
        adversarial_review_required=False,
        opposite_provider_required=False,
    )

    weakenings = ci_risk_classifier.detect_head_classifier_weakenings(base, head)

    assert "lower_risk_class" in weakenings
    assert "lower_complexity_class" in weakenings
    assert "fewer_required_ci_lanes" in weakenings
    assert "fewer_required_reviews" in weakenings
    assert "missing_adversarial_review_required" in weakenings
    assert "missing_opposite_provider_required" in weakenings
    assert "fewer_protected_flags" in weakenings
    assert "fewer_merge_blocking_conditions" in weakenings
    assert "allowed_to_mark_ready_weaker_than_base" in weakenings


def test_authoritative_base_result_gets_execution_metadata_and_blocks_weakened_head() -> None:
    base = classification(risk_class="R3", allowed_to_mark_ready=True, merge_blocking_conditions=[])
    head = classification(risk_class="R2", allowed_to_mark_ready=True, merge_blocking_conditions=[])

    final = ci_risk_classifier.with_trusted_execution_metadata(
        base,
        files=["scripts/ci_risk_classifier.py"],
        repo="neoengine-ai-org/hermes-agent",
        pr_number="7",
        base_sha="base123",
        head_sha="head456",
        classifier_commit="base123",
        head_classifier_result=head,
    )

    assert final["risk_class"] == "R3"
    assert final["allowed_to_mark_ready"] is False
    assert "head_classifier_weakened_posture:lower_risk_class" in final["merge_blocking_conditions"]
    assert final["schema_version"] == "ci-classification.v1"
    assert final["classifier_execution"]["trusted_source"] == "base"
    assert final["classifier_execution"]["repo"] == "neoengine-ai-org/hermes-agent"
    assert final["classifier_execution"]["pr_number"] == "7"
    assert final["classifier_execution"]["classifier_self_change"] is True
    assert final["classifier_execution"]["weakened_by_head_classifier"] is True
    assert final["classifier_execution"]["base_classifier_result"]["risk_class"] == "R3"
    assert final["classifier_execution"]["head_classifier_result"]["risk_class"] == "R2"


def test_cli_emits_artifacts_even_when_fail_closed(tmp_path: Path) -> None:
    pr_body = tmp_path / "body.md"
    pr_body.write_text("plain body without required classification\n", encoding="utf-8")
    output_dir = tmp_path / "out"

    completed = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--changed-file",
            "scripts/ci_risk_classifier.py",
            "--pr-body",
            str(pr_body),
            "--output-dir",
            str(output_dir),
            "--repo",
            "neoengine-ai-org/hermes-agent",
            "--pr-number",
            "7",
            "--base-sha",
            "base123",
            "--head-sha",
            "head456",
            "--classifier-commit",
            "base123",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    data = json.loads((output_dir / "ci-classification.json").read_text(encoding="utf-8"))
    assert (output_dir / "ci-classification.md").exists()
    assert data["schema_version"] == "ci-classification.v1"
    assert data["classifier_execution"]["repo"] == "neoengine-ai-org/hermes-agent"
    assert data["classifier_execution"]["classifier_self_change"] is True
    assert "missing_required_pr_body_classification_section" in data["merge_blocking_conditions"]
