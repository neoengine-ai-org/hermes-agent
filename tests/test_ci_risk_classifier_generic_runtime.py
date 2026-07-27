from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ci_risk_classifier.py"
spec = importlib.util.spec_from_file_location("ci_risk_classifier_generic_runtime", MODULE_PATH)
assert spec is not None
ci_risk_classifier = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = ci_risk_classifier
spec.loader.exec_module(ci_risk_classifier)


def docs_only_declaration() -> str:
    return """## Risk, Complexity, Review, and CI Classification

- Risk class: R0
- Complexity class: C0
- Impacted surfaces: docs_only
- RuntimePayloadContract present: no
- protected_surface: false
- runtime_authority_change: false
- customer_data_or_finance_impact: false
- governance_or_merge_authority_change: false
- model_tier_required: 0
- cc_review_required: false
- opposite_frontier_required: false
- escalation_reason: none
- Blocker exemption, if any: N/A
- Secondary review required: no
- Adversarial review required: no
- Opposite-provider adversarial required: no
- Human/protected review required: no
- Founder review required: no
- Required CI lanes: pr_body_contract, diff_check, docs_impact
- Skipped CI lanes and rationale: runtime lanes skipped for docs-only change
- Token class: S
- Expected state change: documentation only
- Stop condition: classification emitted
"""


def test_unknown_packaged_python_cannot_fall_back_to_docs_only() -> None:
    result = ci_risk_classifier.classify(
        ["lifecycle_contract/__init__.py"],
        docs_only_declaration(),
        additions=200,
        pr_number="67",
        repo="neoengine-ai-org/hermes-agent",
        title="feat: add packaged lifecycle contract",
    )

    assert set(result.impacted_surfaces) == {"runtime_backend"}
    assert result.risk_class == "R2"
    assert result.model_tier_required == 2
    assert result.runtime_payload_contract_present is False
    assert "runtime_surface_without_runtimePayloadContract" in result.merge_blocking_conditions
    assert "declared_model_tier_weaker_than_classifier" in result.merge_blocking_conditions
    assert any(
        blocker.startswith("declared_ci_lanes_weaker_than_classifier:")
        for blocker in result.merge_blocking_conditions
    )
    assert result.allowed_to_mark_ready is False


def test_test_only_unknown_python_does_not_gain_runtime_backend() -> None:
    result = ci_risk_classifier.classify(
        ["tests/test_new_package.py"],
        docs_only_declaration(),
        additions=50,
    )

    assert "test_only" in result.impacted_surfaces
    assert "runtime_backend" not in result.impacted_surfaces


def test_dot_prefixed_workflow_path_keeps_tooling_classification() -> None:
    surfaces = ci_risk_classifier.infer_surfaces(
        [".github/scripts/new_policy_check.py"],
        "",
    )

    assert "ci_workflow" in surfaces
    assert "runtime_backend" not in surfaces


def test_exact_dot_slash_prefix_is_removed_without_erasing_dot_directory() -> None:
    assert ci_risk_classifier._is_package_like_unknown_executable(
        "./lifecycle_contract/__init__.py"
    )
    assert not ci_risk_classifier._is_package_like_unknown_executable(
        ".github/scripts/new_policy_check.py"
    )
