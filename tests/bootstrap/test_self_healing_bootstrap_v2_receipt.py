from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts/emit_hermes_self_healing_bootstrap_v2_receipt.py"
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


def test_candidate_receipt_binds_protected_source_policy_and_proof():
    receipt = MODULE.build_receipt(
        root=ROOT,
        environment=environment(),
        proof_suite_result="PASS",
    )
    assert receipt["state"] == "HERMES_SELF_HEALING_BOOTSTRAP_V2_CANDIDATE"
    assert receipt["canonical"] is False
    assert receipt["source_subject"] == {
        "commit": MODULE.SOURCE_SUBJECT,
        "tree": MODULE.SOURCE_TREE,
    }
    assert receipt["source_under_test"]["strict_descendant_or_subject"] is True
    assert receipt["policy"]["shared_home_authority_allowed"] is False
    assert receipt["policy"]["fixed_operation"] == {
        "retry_limit": 1,
        "timeout_seconds": 180,
    }
    assert receipt["recovery_proof"]["result"] == "PASS"
    assert receipt["recovery_proof"]["exactly_one_retry"] == "PASS"
    assert receipt["publication"]["candidate_artifact_only"] is True
    assert receipt["publication"]["requires_strict_descendant_receipt_pr"] is True


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


def test_wrong_or_unrelated_source_subject_fails_closed():
    with pytest.raises(ValueError, match="source subject"):
        MODULE.build_receipt(
            root=ROOT,
            environment=environment(),
            proof_suite_result="PASS",
            source_subject="not-a-commit",
        )
    with pytest.raises(ValueError, match="not an ancestor"):
        MODULE.build_receipt(
            root=ROOT,
            environment=environment(),
            proof_suite_result="PASS",
            source_subject="f" * 40,
        )


def test_workflow_identity_and_pass_result_are_mandatory():
    invalid = environment()
    invalid["GITHUB_SHA"] = "a" * 40
    with pytest.raises(ValueError, match="workflow or proof-suite"):
        MODULE.build_receipt(
            root=ROOT,
            environment=invalid,
            proof_suite_result="PASS",
        )
    with pytest.raises(ValueError, match="workflow or proof-suite"):
        MODULE.build_receipt(
            root=ROOT,
            environment=environment(),
            proof_suite_result="FAIL",
        )
