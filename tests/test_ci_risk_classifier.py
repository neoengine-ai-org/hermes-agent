from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ci_risk_classifier.py"
spec = importlib.util.spec_from_file_location("ci_risk_classifier", MODULE_PATH)
assert spec is not None
ci_risk_classifier = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = ci_risk_classifier
spec.loader.exec_module(ci_risk_classifier)


def body(**overrides: str) -> str:
    fields = {
        "Risk class": "R0",
        "Complexity class": "C0",
        "Impacted surfaces": "docs_only",
        "runtime_payload_required": "false",
        "RuntimePayloadContract present": "no",
        "Blocker exemption, if any": "N/A",
        "Secondary review required": "no",
        "Adversarial review required": "no",
        "Opposite-provider adversarial required": "no",
        "Human/protected review required": "no",
        "Founder review required": "no",
        "Required CI lanes": "pr_body_contract, diff_check, docs_impact",
        "Skipped CI lanes and rationale": "runtime lanes skipped for docs-only change",
        "Token class": "S",
        "Expected state change": "PR body/docs repair",
        "Stop condition": "classification emitted",
    }
    fields.update(overrides)
    lines = ["## Risk, Complexity, Review, and CI Classification", ""]
    lines.extend(f"- {key}: {value}" for key, value in fields.items())
    return "\n".join(lines) + "\n"


def test_docs_only_pr_is_low_risk_and_ready() -> None:
    result = ci_risk_classifier.classify(
        ["docs/usage.md"],
        body(),
        additions=12,
        pr_number="1",
        repo="neoengine/hermes-agent",
        title="docs: update usage",
    )

    assert result.risk_class == "R0"
    assert result.complexity_class == "C0"
    assert result.token_class == "S"
    assert result.allowed_to_mark_ready is True
    assert result.required_reviews == ["no_secondary_review_required"]


def test_runtime_change_without_contract_blocks_ready() -> None:
    result = ci_risk_classifier.classify(
        ["agent/system_prompt.py"],
        body(
            **{
                "Risk class": "R2",
                "Complexity class": "C2",
                "Impacted surfaces": "runtime_backend",
                "Required CI lanes": "pr_body_contract, diff_check, docs_impact, typecheck, targeted_runtime_tests, backend_runtime, build",
            }
        ),
        additions=80,
    )

    assert result.risk_class == "R2"
    assert result.secondary_review_required is True
    assert result.adversarial_review_required is True
    assert "runtime_surface_without_runtimePayloadContract" in result.merge_blocking_conditions
    assert result.allowed_to_mark_ready is False


def test_runtime_payload_required_requires_executable_payload_details() -> None:
    result = ci_risk_classifier.classify(
        ["agent/system_prompt.py"],
        body(
            **{
                "Risk class": "R2",
                "Complexity class": "C2",
                "Impacted surfaces": "runtime_backend",
                "runtime_payload_required": "true",
                "RuntimePayloadContract present": "yes",
                "Required CI lanes": "pr_body_contract, diff_check, docs_impact, typecheck, targeted_runtime_tests, backend_runtime, build",
            }
        ),
        additions=80,
    )

    assert result.runtime_payload_required is True
    assert "runtime_payload_required_without_product_runtime_artifact" in result.merge_blocking_conditions
    assert "runtime_payload_required_without_changed_runtime_product_files" in result.merge_blocking_conditions
    assert "runtime_payload_required_without_behavior_tests" in result.merge_blocking_conditions
    assert "runtime_payload_required_without_validation_commands" in result.merge_blocking_conditions
    assert "runtime_payload_required_without_explicit_non_claims" in result.merge_blocking_conditions
    assert result.allowed_to_mark_ready is False


def test_runtime_payload_required_accepts_complete_executable_payload_details() -> None:
    result = ci_risk_classifier.classify(
        ["agent/system_prompt.py"],
        body(
            **{
                "Risk class": "R2",
                "Complexity class": "C2",
                "Impacted surfaces": "runtime_backend",
                "runtime_payload_required": "true",
                "RuntimePayloadContract present": "yes",
                "Product/runtime artifact": "shared system-prompt enforcement code",
                "Changed runtime/product files": "agent/system_prompt.py",
                "Behavior tests": "tests/agent/test_system_prompt.py",
                "Validation commands": "python -m pytest tests/agent/test_system_prompt.py -q",
                "Explicit non-claims": "does not claim launch/protected approval",
                "Required CI lanes": "pr_body_contract, diff_check, docs_impact, typecheck, targeted_runtime_tests, backend_runtime, build",
            }
        ),
        additions=80,
    )

    assert result.runtime_payload_required is True
    assert not any(item.startswith("runtime_payload_required_without_") for item in result.merge_blocking_conditions)


def test_protected_surface_requires_human_and_non_claims() -> None:
    result = ci_risk_classifier.classify(
        ["gateway/auth/token_vault.py"],
        body(
            **{
                "Risk class": "R4",
                "Complexity class": "C3",
                "Impacted surfaces": "secrets_tokens, auth_identity",
                "RuntimePayloadContract present": "yes",
                "Required CI lanes": "pr_body_contract, diff_check, docs_impact, typecheck, targeted_runtime_tests, backend_runtime, build, governance_required, security_required, protected_claim_gate, human_gate_required, privacy_data_gate, rollback_proof, audit_log_validation, auth_tests, session_isolation_tests, secret_scan, token_storage_tests, encryption_tests, protected_human_review",
            }
        ),
        additions=120,
    )

    assert result.risk_class == "R4"
    assert result.human_gate_required is True
    assert result.opposite_provider_required is True
    assert "protected_surface_without_protected_non_claims" in result.merge_blocking_conditions


def test_declared_lanes_weaker_than_classifier_blocks_ready() -> None:
    result = ci_risk_classifier.classify(
        [".github/workflows/tests.yml"],
        body(
            **{
                "Risk class": "R3",
                "Complexity class": "C2",
                "Impacted surfaces": "ci_workflow",
                "Required CI lanes": "pr_body_contract, diff_check",
            }
        ),
        additions=20,
    )

    assert result.risk_class == "R3"
    assert any(item.startswith("declared_ci_lanes_weaker_than_classifier:") for item in result.merge_blocking_conditions)
    assert "workflow_lint" in result.required_ci_lanes
    assert "pr_impact_classifier_tests" in result.required_ci_lanes


def test_prd_packet_requires_runtime_first_maturity_and_ralph_fields() -> None:
    result = ci_risk_classifier.classify(
        ["packets/cto/prd-runtime.json"],
        body(
            **{
                "Risk class": "R2",
                "Complexity class": "C2",
                "Impacted surfaces": "prd_packet",
                "runtime_payload_required": "true",
                "RuntimePayloadContract present": "yes",
                "Product/runtime artifact": "prd packet compiler",
                "Changed runtime/product files": "packets/cto/prd-runtime.json",
                "Behavior tests": "tests/prd/test_runtime_contract.py",
                "Validation commands": "pytest tests/prd/test_runtime_contract.py -q",
                "Explicit non-claims": "does not claim product completion",
                "Required CI lanes": "pr_body_contract, diff_check, docs_impact, typecheck, targeted_runtime_tests, backend_runtime, build, validate_prd_packets, runtimePayloadContract_validation, blocker_exemption_validation, ralph_convergence_validation, runtime_maturity_validation",
            }
        ),
        additions=80,
    )

    assert "prd_packet_without_maturityLevelTarget" in result.merge_blocking_conditions
    assert "prd_packet_without_currentMaturityLevel" in result.merge_blocking_conditions
    assert "prd_packet_without_targetMaturityLevel" in result.merge_blocking_conditions
    assert "prd_packet_without_ralphLoopStage" in result.merge_blocking_conditions
    assert "prd_packet_without_productSurfaceTarget" in result.merge_blocking_conditions
    assert "prd_packet_without_runtimeBehaviorDelta" in result.merge_blocking_conditions
    assert "prd_packet_without_userOrSystemVisibleDelta" in result.merge_blocking_conditions
    assert "prd_packet_without_validationTestDelta" in result.merge_blocking_conditions
    assert "prd_packet_without_runtimeProofRequired" in result.merge_blocking_conditions
    assert "prd_packet_without_finiteCloseCondition" in result.merge_blocking_conditions
    assert "prd_packet_without_nonClaims" in result.merge_blocking_conditions
    assert "prd_packet_without_nextRuntimePacketRecommendation" in result.merge_blocking_conditions



def test_prd_runtime_payload_contract_keyword_in_ci_lane_is_not_enough() -> None:
    result = ci_risk_classifier.classify(
        ["packets/cto/prd-runtime.json"],
        body(
            **{
                "Risk class": "R2",
                "Complexity class": "C2",
                "Impacted surfaces": "prd_packet",
                "runtime_payload_required": "true",
                "RuntimePayloadContract present": "no",
                "Product/runtime artifact": "prd packet compiler",
                "Changed runtime/product files": "packets/cto/prd-runtime.json",
                "Behavior tests": "tests/prd/test_runtime_contract.py",
                "Validation commands": "pytest tests/prd/test_runtime_contract.py -q",
                "Explicit non-claims": "does not claim product completion",
                "maturityLevelTarget": "v3",
                "currentMaturityLevel": "v2",
                "targetMaturityLevel": "v3",
                "ralphLoopStage": "runtime_proof_pass",
                "productSurfaceTarget": "cto_prd_packet_compiler",
                "runtimeBehaviorDelta": "invalid packet input fails closed before routing",
                "userOrSystemVisibleDelta": "Conductor sees rejected packet status",
                "validationTestDelta": "accepted and rejected PRD JSON cases",
                "runtimeProofRequired": "contract validation test artifact",
                "finiteCloseCondition": "validator accepts valid v3 and rejects missing contract",
                "supportWorkClassification": "N/A",
                "runtimeBlockerExemption": "N/A",
                "nonClaims": "does not claim v4 surface",
                "nextRuntimePacketRecommendation": "route v4 surface projection packet",
                "Required CI lanes": "pr_body_contract, diff_check, docs_impact, typecheck, targeted_runtime_tests, backend_runtime, build, validate_prd_packets, runtimePayloadContract_validation, blocker_exemption_validation, ralph_convergence_validation, runtime_maturity_validation",
            }
        ),
        additions=80,
    )

    assert "prd_packet_without_runtimePayloadContract" in result.merge_blocking_conditions

def test_prd_packet_accepts_complete_runtime_first_v3_ralph_contract() -> None:
    result = ci_risk_classifier.classify(
        ["packets/cto/prd-runtime.json"],
        body(
            **{
                "Risk class": "R2",
                "Complexity class": "C2",
                "Impacted surfaces": "prd_packet",
                "runtime_payload_required": "true",
                "RuntimePayloadContract present": "yes",
                "Product/runtime artifact": "prd packet compiler",
                "Changed runtime/product files": "packets/cto/prd-runtime.json",
                "Behavior tests": "tests/prd/test_runtime_contract.py",
                "Validation commands": "pytest tests/prd/test_runtime_contract.py -q",
                "Explicit non-claims": "does not claim product completion",
                "maturityLevelTarget": "v3",
                "currentMaturityLevel": "v2",
                "targetMaturityLevel": "v3",
                "ralphLoopStage": "runtime_proof_pass",
                "productSurfaceTarget": "cto_prd_packet_compiler",
                "runtimeBehaviorDelta": "invalid packet input fails closed before routing",
                "userOrSystemVisibleDelta": "Conductor sees rejected packet status",
                "validationTestDelta": "accepted and rejected PRD JSON cases",
                "runtimeProofRequired": "contract validation test artifact",
                "finiteCloseCondition": "validator accepts valid v3 and rejects missing contract",
                "supportWorkClassification": "N/A",
                "runtimeBlockerExemption": "N/A",
                "nonClaims": "does not claim v4 surface",
                "nextRuntimePacketRecommendation": "route v4 surface projection packet",
                "Required CI lanes": "pr_body_contract, diff_check, docs_impact, typecheck, targeted_runtime_tests, backend_runtime, build, validate_prd_packets, runtimePayloadContract_validation, blocker_exemption_validation, ralph_convergence_validation, runtime_maturity_validation",
            }
        ),
        additions=80,
    )

    assert not any(item.startswith("prd_packet_without_") for item in result.merge_blocking_conditions)
    assert result.maturity_level_target == "v3"
    assert result.ralph_loop_stage == "runtime_proof_pass"
    assert "runtime_maturity_validation" in result.required_ci_lanes
    assert "ralph_convergence_validation" in result.required_ci_lanes


def test_support_only_packet_must_bind_to_runtime_packet() -> None:
    result = ci_risk_classifier.classify(
        ["docs/governance/maturity-rubric.md"],
        body(
            **{
                "Risk class": "R0",
                "Complexity class": "C0",
                "Impacted surfaces": "docs_only",
                "supportWorkClassification": "governance-only",
                "finiteCloseCondition": "doc updated",
            }
        ),
        additions=40,
    )

    assert "support_work_not_bound_to_runtime_packet" in result.merge_blocking_conditions
    assert result.allowed_to_mark_ready is False


def test_v6_or_higher_requires_runtime_intelligence_delta() -> None:
    result = ci_risk_classifier.classify(
        ["packets/cto/prd-runtime.json"],
        body(
            **{
                "Risk class": "R2",
                "Complexity class": "C2",
                "Impacted surfaces": "prd_packet",
                "runtime_payload_required": "true",
                "RuntimePayloadContract present": "yes",
                "Product/runtime artifact": "packet text",
                "Changed runtime/product files": "packets/cto/prd-runtime.json",
                "Behavior tests": "tests/prd/test_runtime_contract.py",
                "Validation commands": "pytest tests/prd/test_runtime_contract.py -q",
                "Explicit non-claims": "does not claim decisioning",
                "maturityLevelTarget": "v6",
                "currentMaturityLevel": "v5",
                "targetMaturityLevel": "v6",
                "ralphLoopStage": "runtime_proof_pass",
                "productSurfaceTarget": "cto_packet",
                "runtimeBehaviorDelta": "packet text improves",
                "userOrSystemVisibleDelta": "packet body changes",
                "validationTestDelta": "snapshot updated",
                "runtimeProofRequired": "snapshot",
                "finiteCloseCondition": "snapshot updated",
                "supportWorkClassification": "N/A",
                "runtimeBlockerExemption": "N/A",
                "nonClaims": "does not add decisioning",
                "nextRuntimePacketRecommendation": "add routing classifier later",
                "Required CI lanes": "pr_body_contract, diff_check, docs_impact, typecheck, targeted_runtime_tests, backend_runtime, build, validate_prd_packets, runtimePayloadContract_validation, blocker_exemption_validation, ralph_convergence_validation, runtime_maturity_validation",
            }
        ),
        additions=80,
    )

    assert "v6_target_without_runtime_intelligence_delta" in result.merge_blocking_conditions
