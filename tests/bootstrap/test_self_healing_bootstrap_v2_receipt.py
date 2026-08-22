from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest  # ty: ignore[unresolved-import]


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts/emit_hermes_self_healing_bootstrap_v2_receipt.py"
WORKFLOW_PATH = ROOT / ".github/workflows/self-healing-bootstrap-v2-receipt.yml"
SPEC = importlib.util.spec_from_file_location(
    "emit_hermes_self_healing_bootstrap_v2_receipt", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def environment():
    head = MODULE.git(ROOT, "rev-parse", "HEAD")
    return {
        "GITHUB_REPOSITORY": MODULE.REPOSITORY,
        "GITHUB_WORKFLOW": MODULE.WORKFLOW_NAME,
        "GITHUB_SHA": head,
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_RUN_ID": "101",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_JOB": "self-healing-bootstrap-v2-receipt",
        "GITHUB_REF_NAME": "feature",
    }


def current_subject() -> tuple[str, str]:
    return (
        MODULE.git(ROOT, "rev-parse", "HEAD"),
        MODULE.git(ROOT, "rev-parse", "HEAD^{tree}"),
    )


def passing_junit(path: Path) -> Path:
    nodes = sorted(MODULE.JUNIT_BOUND_NODES)
    cases = "".join(
        f'<testcase classname="{node.split(".py::", 1)[0].replace("/", ".")}" '
        f'name="{node.split("::", 1)[1]}" />'
        for node in nodes
    )
    path.write_text(
        f'<testsuites><testsuite tests="{len(nodes)}" failures="0" errors="0" skipped="0">'
        f"{cases}</testsuite></testsuites>"
    )
    return path


def current_base_receipt() -> dict:
    head, tree = current_subject()
    dependencies = []
    for dependency_id, relative in MODULE.BASE_DEPENDENCIES.items():
        blob_sha, blob = MODULE.head_blob(ROOT, relative)
        dependencies.append(
            {
                "dependency_id": dependency_id,
                "dependency_class": "ORG_NATIVE_BOOTSTRAP",
                "canonical_path_or_provider": relative,
                "local_required": True,
                "status": "READY",
                "digest": MODULE.sha256(blob),
                "version": "1",
                "blob_sha": blob_sha,
            }
        )
    manifest_blob_sha, manifest_blob = MODULE.head_blob(
        ROOT, MODULE.BASE_MANIFEST_REL.as_posix()
    )
    validator_blob_sha, validator_blob = MODULE.head_blob(
        ROOT, MODULE.BASE_VALIDATOR_REL.as_posix()
    )
    payload = {
        "schema_version": "hermes.bootstrap-closure-receipt/1.0",
        "emitter_repository": {
            "full_name": MODULE.REPOSITORY,
            "default_branch": "main",
        },
        "source": {
            "head": head,
            "tree": tree,
            "lock_sha256": MODULE.sha256((ROOT / "uv.lock").read_bytes()),
        },
        "manifest": {
            "path": MODULE.BASE_MANIFEST_REL.as_posix(),
            "blob_sha": manifest_blob_sha,
            "sha256": MODULE.sha256(manifest_blob),
            "schema_version": "hermes.bootstrap-closure-manifest/1.0",
        },
        "validator": {
            "path": MODULE.BASE_VALIDATOR_REL.as_posix(),
            "blob_sha": validator_blob_sha,
            "sha256": MODULE.sha256(validator_blob),
            "version": "1.0.0",
        },
        "dependencies": dependencies,
        "interpreter": {"classification": "ORG_NATIVE_BOOTSTRAP"},
        "environment": {"network": "DISABLED_FOR_SMOKE"},
        "proofs": {"clean_checkout": "required_by_ci_and_detached_proof"},
        "result_state": "HERMES_BOOTSTRAP_CLOSURE_READY",
        "coverage": "candidate_checkout",
        "typed_omissions": sorted(MODULE.BASE_TYPED_OMISSIONS),
        "rollback": "test fixture",
        "non_claims": ["test fixture has no authority"],
    }
    digest = MODULE.canonical_digest(payload)
    return {
        **payload,
        "receipt_id": f"hermes-bootstrap-{digest[:20]}",
        "canonical_payload_digest": digest,
    }


def test_candidate_receipt_binds_policy_and_unit_proof_in_shallow_checkout(tmp_path):
    # Repository-wide shards use a depth-1 PR merge checkout. The unit proof uses
    # the current exact commit as its synthetic source; the dedicated full-history
    # workflow uses the protected source constants and is receipt-grade.
    source_subject, source_tree = current_subject()
    receipt = MODULE.build_receipt(
        root=ROOT,
        environment=environment(),
        proof_suite_result="PASS",
        base_receipt=current_base_receipt(),
        proof_observation=MODULE.observed_proof(passing_junit(tmp_path / "proof.xml")),
        base_receipt_path="artifacts/bootstrap/hermes-bootstrap-closure-receipt-v1.json",
        source_subject=source_subject,
        expected_source_tree=source_tree,
    )
    assert receipt["state"] == "HERMES_SELF_HEALING_BOOTSTRAP_V2_CANDIDATE"
    assert receipt["canonical"] is False
    assert receipt["source_subject"] == {
        "commit": source_subject,
        "tree": source_tree,
    }
    assert receipt["base_closure"]["result_state"] == (
        "HERMES_BOOTSTRAP_CLOSURE_READY"
    )
    assert receipt["base_closure"]["dependency_count"] == len(
        MODULE.BASE_DEPENDENCIES
    )
    assert receipt["source_under_test"]["strict_descendant_or_subject"] is True
    assert receipt["policy"]["shared_home_authority_allowed"] is False
    assert receipt["policy"]["fixed_operation"] == {
        "retry_limit": 1,
        "timeout_seconds": 180,
    }
    assert receipt["recovery_proof"]["result"] == "PASS"
    assert receipt["recovery_proof"]["base_v1_closure"] == "PASS"
    assert receipt["recovery_proof"]["single_attempt_on_success"] == "PASS"
    assert set(receipt["recovery_proof"]["proof_bindings"]) == {
        "abrupt_exit_lock_release",
        "base_v1_closure",
        "corrupt_fingerprinted_environment_rebuilt_once",
        "deleted_stage1_exact_recovery",
        "exact_mode_restoration",
        "single_attempt_on_success",
        "fixed_operation_no_manifest_argv",
        "post_repair_noop",
        "live_owner_preserved",
        "malformed_lock_recovery",
        "receipt_contract_validation",
    }
    assert receipt["recovery_proof"]["proof_suites"] == [
        "tests/bootstrap/test_self_healing_bootstrap_v2.py",
        "tests/bootstrap/test_self_healing_bootstrap_v2_final_hardening.py",
        "tests/bootstrap/test_self_healing_bootstrap_v2_receipt.py",
    ]
    assert receipt["schema_version"] == "hermes.self-healing-bootstrap-receipt/3.0.0"
    assert receipt["recovery_proof"]["proof_bindings"] == {
        "post_repair_noop": "tests/bootstrap/test_self_healing_bootstrap_v2_final_hardening.py::test_fingerprinted_corrupt_environment_rebuilds_once",
        "deleted_stage1_exact_recovery": "tests/bootstrap/test_self_healing_bootstrap_v2.py::test_stage0_restores_deleted_resolver_and_rejects_wrong_pin",
        "exact_mode_restoration": "tests/bootstrap/test_self_healing_bootstrap_v2.py::test_stage0_restores_deleted_resolver_and_rejects_wrong_pin",
        "malformed_lock_recovery": "tests/bootstrap/test_self_healing_bootstrap_v2_final_hardening.py::test_malformed_lock_payload_recovers_and_persists",
        "abrupt_exit_lock_release": "tests/bootstrap/test_self_healing_bootstrap_v2_final_hardening.py::test_abrupt_exit_releases_kernel_lock",
        "live_owner_preserved": "tests/bootstrap/test_self_healing_bootstrap_v2_final_hardening.py::test_live_owner_is_preserved",
        "corrupt_fingerprinted_environment_rebuilt_once": "tests/bootstrap/test_self_healing_bootstrap_v2_final_hardening.py::test_fingerprinted_corrupt_environment_rebuilds_once",
        "single_attempt_on_success": "tests/bootstrap/test_self_healing_bootstrap_v2_final_hardening.py::test_fingerprinted_corrupt_environment_rebuilds_once",
        "fixed_operation_no_manifest_argv": "tests/bootstrap/test_self_healing_bootstrap_v2_final_hardening.py::test_policy_pin_fixed_operation_and_final_entrypoint",
        "base_v1_closure": "validated-base-receipt:artifacts/bootstrap/hermes-bootstrap-closure-receipt-v1.json",
        "receipt_contract_validation": "tests/bootstrap/test_self_healing_bootstrap_v2_receipt.py::test_candidate_receipt_binds_policy_and_unit_proof_in_shallow_checkout",
    }
    assert receipt["publication"]["candidate_artifact_only"] is True
    assert receipt["publication"]["requires_protected_commit_check_readback"] is True
    assert receipt["publication"]["requires_exact_artifact_member_verification"] is True
    assert receipt["publication"]["requires_strict_descendant_receipt_pr"] is True


def test_junit_bindings_reject_missing_skipped_and_failed_nodes(tmp_path):
    report = passing_junit(tmp_path / "proof.xml")
    observation = MODULE.observed_proof(report)
    assert observation.result == "PASS"
    assert observation.claims == {claim: "PASS" for claim in MODULE.PROOF_BINDINGS}

    report.write_text('<testsuites><testsuite tests="0" failures="0" errors="0" skipped="0" /></testsuites>')
    with pytest.raises(ValueError, match="coverage|missing|at least|completely"):
        MODULE.observed_proof(report)

    node = sorted(MODULE.JUNIT_BOUND_NODES)[0]
    raw = passing_junit(report).read_text()
    raw = raw.replace(
        f'name="{node.split("::", 1)[1]}" />',
        f'name="{node.split("::", 1)[1]}"><skipped /></testcase>',
        1,
    ).replace('skipped="0"', 'skipped="1"', 1)
    report.write_text(raw)
    with pytest.raises(ValueError, match="did not pass completely") as skipped:
        MODULE.observed_proof(report)
    assert "'skipped': 1" in str(skipped.value)


def test_base_receipt_path_binding_rejects_relocation(tmp_path):
    source_subject, source_tree = current_subject()
    with pytest.raises(ValueError, match="base receipt path"):
        MODULE.build_receipt(
            root=ROOT,
            environment=environment(),
            proof_suite_result="PASS",
            base_receipt=current_base_receipt(),
            proof_observation=MODULE.observed_proof(
                passing_junit(tmp_path / "proof.xml")
            ),
            base_receipt_path="artifacts/bootstrap/relocated.json",
            source_subject=source_subject,
            expected_source_tree=source_tree,
        )


def test_protected_source_constants_are_exact():
    assert MODULE.SOURCE_SUBJECT == "518c00b34eb2df7f550a0791bb9c5b657ec38071"
    assert MODULE.SOURCE_TREE == "7975b8563ca7d3a58a237cab9bdfc8329c19c408"
    assert MODULE.WORKFLOW_PATH == (
        ".github/workflows/self-healing-bootstrap-v2-receipt.yml"
    )


def test_receipt_workflow_tracks_complete_base_and_v2_surfaces():
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    for relative in (
        *MODULE.BASE_DEPENDENCIES.values(),
        MODULE.POLICY_REL.as_posix(),
        MODULE.PIN_REL.as_posix(),
        "scripts/bootstrap_stage0_v2.py",
        "scripts/bootstrap_resolver_v2_hardened.py",
        "scripts/bootstrap_resolver_v2_final.py",
        "scripts/emit_hermes_self_healing_bootstrap_v2_receipt.py",
        "tests/bootstrap/test_self_healing_bootstrap_v2.py",
        "tests/bootstrap/test_self_healing_bootstrap_v2_final_hardening.py",
        "tests/bootstrap/test_self_healing_bootstrap_v2_receipt.py",
    ):
        assert workflow.count(f"- '{relative}'") == 2, relative


def test_policy_refuses_manifest_supplied_operation_command():
    raw = (ROOT / MODULE.POLICY_REL).read_bytes()
    policy = json.loads(raw)
    policy["operations"]["validate-bootstrap"]["argv"] = ["python", "unsafe.py"]
    mutated = (json.dumps(policy, sort_keys=True) + "\n").encode("utf-8")
    pin = MODULE.sha256(mutated)
    with pytest.raises(ValueError, match="fixed and exactly-once"):
        MODULE.validate_policy(
            policy=policy,
            policy_bytes=mutated,
            pin_text=pin,
        )


def test_policy_pin_mismatch_fails_closed():
    raw = (ROOT / MODULE.POLICY_REL).read_bytes()
    policy = json.loads(raw)
    with pytest.raises(ValueError, match="policy pin"):
        MODULE.validate_policy(
            policy=policy,
            policy_bytes=raw,
            pin_text="f" * 64,
        )


def test_base_receipt_provenance_and_digest_fail_closed():
    source_subject, source_tree = current_subject()
    invalid = current_base_receipt()
    invalid["manifest"]["blob_sha"] = "f" * 40
    payload = {
        key: value
        for key, value in invalid.items()
        if key not in {"receipt_id", "canonical_payload_digest"}
    }
    digest = MODULE.canonical_digest(payload)
    invalid["canonical_payload_digest"] = digest
    invalid["receipt_id"] = f"hermes-bootstrap-{digest[:20]}"
    with pytest.raises(ValueError, match="manifest provenance"):
        MODULE.build_receipt(
            root=ROOT,
            environment=environment(),
            proof_suite_result="PASS",
            base_receipt=invalid,
            source_subject=source_subject,
            expected_source_tree=source_tree,
        )

    invalid = current_base_receipt()
    invalid["canonical_payload_digest"] = "f" * 64
    with pytest.raises(ValueError, match="canonical digest"):
        MODULE.build_receipt(
            root=ROOT,
            environment=environment(),
            proof_suite_result="PASS",
            base_receipt=invalid,
            source_subject=source_subject,
            expected_source_tree=source_tree,
        )


def test_base_receipt_dependency_surface_fails_closed():
    source_subject, source_tree = current_subject()
    invalid = current_base_receipt()
    invalid["dependencies"].pop()
    payload = {
        key: value
        for key, value in invalid.items()
        if key not in {"receipt_id", "canonical_payload_digest"}
    }
    digest = MODULE.canonical_digest(payload)
    invalid["canonical_payload_digest"] = digest
    invalid["receipt_id"] = f"hermes-bootstrap-{digest[:20]}"
    with pytest.raises(ValueError, match="dependency surface"):
        MODULE.build_receipt(
            root=ROOT,
            environment=environment(),
            proof_suite_result="PASS",
            base_receipt=invalid,
            source_subject=source_subject,
            expected_source_tree=source_tree,
        )


def test_wrong_or_unavailable_source_subject_fails_closed():
    source_subject, source_tree = current_subject()
    base_receipt = current_base_receipt()
    with pytest.raises(ValueError, match="source subject"):
        MODULE.build_receipt(
            root=ROOT,
            environment=environment(),
            proof_suite_result="PASS",
            base_receipt=base_receipt,
            source_subject="not-a-commit",
            expected_source_tree=source_tree,
        )
    with pytest.raises(ValueError, match="unavailable|not an ancestor"):
        MODULE.build_receipt(
            root=ROOT,
            environment=environment(),
            proof_suite_result="PASS",
            base_receipt=base_receipt,
            source_subject="f" * 40,
            expected_source_tree=source_tree,
        )
    with pytest.raises(ValueError, match="tree differs"):
        MODULE.build_receipt(
            root=ROOT,
            environment=environment(),
            proof_suite_result="PASS",
            base_receipt=base_receipt,
            source_subject=source_subject,
            expected_source_tree="f" * 40,
        )


def test_workflow_identity_and_pass_result_are_mandatory_before_history_probe():
    source_subject, source_tree = current_subject()
    base_receipt = current_base_receipt()
    invalid = environment()
    invalid["GITHUB_SHA"] = "a" * 40
    with pytest.raises(ValueError, match="workflow or proof-suite"):
        MODULE.build_receipt(
            root=ROOT,
            environment=invalid,
            proof_suite_result="PASS",
            base_receipt=base_receipt,
            source_subject=source_subject,
            expected_source_tree=source_tree,
        )
    with pytest.raises(ValueError, match="workflow or proof-suite"):
        MODULE.build_receipt(
            root=ROOT,
            environment=environment(),
            proof_suite_result="FAIL",
            base_receipt=base_receipt,
            source_subject=source_subject,
            expected_source_tree=source_tree,
        )
