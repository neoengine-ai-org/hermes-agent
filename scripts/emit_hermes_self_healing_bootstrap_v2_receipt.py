#!/usr/bin/env python3
"""Emit a candidate receipt for the merged Hermes self-healing bootstrap V2."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any
import xml.etree.ElementTree as ET


REPOSITORY = "neoengine-ai-org/hermes-agent"
WORKFLOW_NAME = "Hermes self-healing bootstrap V2 receipt"
WORKFLOW_PATH = ".github/workflows/self-healing-bootstrap-v2-receipt.yml"
SOURCE_SUBJECT = "518c00b34eb2df7f550a0791bb9c5b657ec38071"
SOURCE_TREE = "7975b8563ca7d3a58a237cab9bdfc8329c19c408"
POLICY_REL = Path("config/hermes-bootstrap-acquisition-v2.json")
PIN_REL = Path("config/hermes-bootstrap-acquisition-v2.sha256")
BASE_MANIFEST_REL = Path("config/hermes-bootstrap-closure-v1.json")
BASE_VALIDATOR_REL = Path("scripts/validate_hermes_bootstrap_closure.py")
SHA40 = re.compile(r"^[a-f0-9]{40}$")
SHA64 = re.compile(r"^[a-f0-9]{64}$")
RECEIPT_ID = re.compile(r"^hermes-bootstrap-[a-f0-9]{20}$")
BASE_TYPED_OMISSIONS = {
    "protected_main_live_verification",
    "ci_run_identity",
    "independent_exact_head_review",
}
BASE_DEPENDENCIES = {
    "ci-admission": ".github/workflows/tests.yml",
    "agent-instructions": "AGENTS.md",
    "readme": "README.md",
    "acp-entrypoint": "acp_adapter/entry.py",
    "bootstrap-manifest": "config/hermes-bootstrap-closure-v1.json",
    "windows-bootstrap": "hermes_bootstrap.py",
    "path-constants": "hermes_constants.py",
    "cli-entrypoint": "hermes_cli/main.py",
    "package-metadata": "pyproject.toml",
    "agent-entrypoint": "run_agent.py",
    "progress-entrypoint": "scripts/hermes_progress.py",
    "bootstrap-stage0": "scripts/bootstrap_stage0_v2.py",
    "test-runner": "scripts/run_tests.sh",
    "test-runner-v1": "scripts/run_tests_v1.sh",
    "parallel-test-runner": "scripts/run_tests_parallel.py",
    "bootstrap-validator": "scripts/validate_hermes_bootstrap_closure.py",
    "hostile-tests": "tests/bootstrap/test_bootstrap_closure.py",
    "lockfile": "uv.lock",
}
PROOF_BINDINGS = {
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
PROOF_SUITES = frozenset(
    {
        "tests/bootstrap/test_self_healing_bootstrap_v2.py",
        "tests/bootstrap/test_self_healing_bootstrap_v2_final_hardening.py",
        "tests/bootstrap/test_self_healing_bootstrap_v2_receipt.py",
    }
)
if not set(PROOF_BINDINGS).isdisjoint(
    {"result", "proof_bindings", "proof_binding_semantics", "proof_suites"}
):
    raise RuntimeError("proof claim names collide with reserved receipt keys")
JUNIT_BOUND_NODES = frozenset(
    binding for binding in PROOF_BINDINGS.values() if binding.startswith("tests/")
)


@dataclass(frozen=True)
class ObservedProof:
    result: str
    claims: dict[str, str]
    suites: tuple[str, ...]
    junit_sha256: str


def observed_proof(junit_path: Path) -> ObservedProof:
    root = ET.parse(junit_path).getroot()
    suite_rows = list(root.iter("testsuite"))
    if not suite_rows:
        raise ValueError("JUnit contains no test suite")
    totals = {name: 0 for name in ("tests", "failures", "errors", "skipped")}
    for suite in suite_rows:
        for name in totals:
            raw = suite.get(name)
            if raw is None or not raw.isdecimal():
                raise ValueError(f"JUnit suite total is absent or malformed: {name}")
            totals[name] += int(raw)
    if totals["tests"] < 1 or any(totals[name] != 0 for name in ("failures", "errors", "skipped")):
        raise ValueError(f"JUnit suite did not pass completely: {totals}")
    outcomes: dict[str, str] = {}
    observed_suites: set[str] = set()
    testcase_count = 0
    for case in root.iter("testcase"):
        testcase_count += 1
        classname = case.get("classname", "")
        name = case.get("name", "")
        node = f"{classname.replace('.', '/')}.py::{name}"
        observed_suites.add(node.split("::", 1)[0])
        if node not in JUNIT_BOUND_NODES:
            continue
        if node in outcomes:
            raise ValueError(f"duplicate bound proof node in JUnit: {node}")
        outcome = "PASS"
        if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            outcome = "NON_PASS"
        outcomes[node] = outcome
    missing = sorted(JUNIT_BOUND_NODES - set(outcomes))
    non_pass = sorted(node for node, outcome in outcomes.items() if outcome != "PASS")
    if missing or non_pass:
        raise ValueError(f"bound proof nodes did not execute and pass (missing={missing}, non_pass={non_pass})")
    if testcase_count != totals["tests"] or observed_suites != PROOF_SUITES:
        raise ValueError(
            f"JUnit suite coverage differs (count={testcase_count}, suites={sorted(observed_suites)})"
        )
    claims = {
        claim: ("PASS" if binding.startswith("validated-base-receipt:") else outcomes[binding])
        for claim, binding in PROOF_BINDINGS.items()
    }
    return ObservedProof(
        "PASS",
        claims,
        tuple(sorted(observed_suites)),
        hashlib.sha256(junit_path.read_bytes()).hexdigest(),
    )


def git(root: Path, *arguments: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        text=True,
        capture_output=True,
    )
    if check and completed.returncode != 0:
        raise ValueError(completed.stderr.strip() or "git command failed")
    return completed.stdout.strip()


def git_bytes(root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise ValueError(completed.stderr.decode("utf-8", errors="replace").strip() or "git command failed")
    return completed.stdout


def git_returncode(root: Path, *arguments: str) -> int:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def head_blob(root: Path, relative: str) -> tuple[str, bytes]:
    blob_sha = git(root, "rev-parse", f"HEAD:{relative}")
    if not SHA40.fullmatch(blob_sha):
        raise ValueError(f"HEAD blob identity is invalid: {relative}")
    return blob_sha, git_bytes(root, "cat-file", "blob", blob_sha)


def validate_policy(
    *,
    policy: dict[str, Any],
    policy_bytes: bytes,
    pin_text: str,
) -> dict[str, Any]:
    expected = pin_text.strip().split()[0]
    if not SHA64.fullmatch(expected) or sha256(policy_bytes) != expected:
        raise ValueError("policy pin is not exact")
    if policy.get("repository") != REPOSITORY:
        raise ValueError("policy repository identity differs")
    if policy.get("shared_home_authority_allowed") is not False:
        raise ValueError("shared-home authority must remain false")
    operation = policy.get("operations", {}).get("validate-bootstrap")
    if (
        not isinstance(operation, dict)
        or operation != {"retry_limit": 1, "timeout_seconds": 180}
        or "argv" in operation
    ):
        raise ValueError("validation operation is not fixed and exactly-once")
    for name in ("stage0", "stage1"):
        row = policy.get(name)
        if (
            not isinstance(row, dict)
            or not SHA40.fullmatch(str(row.get("blob_sha", "")))
            or not isinstance(row.get("path"), str)
            or not row["path"]
        ):
            raise ValueError(f"{name} identity is invalid")
    return {"sha256": expected, "operation": operation}


def validate_workflow_identity(
    *,
    environment: dict[str, str],
    head: str,
    proof_suite_result: str,
) -> tuple[int, int]:
    run_id = environment.get("GITHUB_RUN_ID", "")
    run_attempt = environment.get("GITHUB_RUN_ATTEMPT", "")
    if (
        environment.get("GITHUB_REPOSITORY") != REPOSITORY
        or environment.get("GITHUB_WORKFLOW") != WORKFLOW_NAME
        or environment.get("GITHUB_SHA") != head
        or environment.get("GITHUB_EVENT_NAME")
        not in {"pull_request", "push", "workflow_dispatch"}
        or not run_id.isdigit()
        or int(run_id) <= 0
        or not run_attempt.isdigit()
        or int(run_attempt) <= 0
        or proof_suite_result != "PASS"
    ):
        raise ValueError("workflow or proof-suite identity is incomplete")
    return int(run_id), int(run_attempt)


def validate_source_ancestry(
    *,
    root: Path,
    head: str,
    source_subject: str,
    expected_source_tree: str,
) -> str:
    if not SHA40.fullmatch(source_subject):
        raise ValueError("source subject is not an exact commit identity")
    if not SHA40.fullmatch(expected_source_tree):
        raise ValueError("expected source tree is not an exact tree identity")
    if git_returncode(root, "cat-file", "-e", f"{source_subject}^{{commit}}") != 0:
        raise ValueError("protected source subject commit is unavailable")
    if git_returncode(root, "merge-base", "--is-ancestor", source_subject, head) != 0:
        raise ValueError("protected source subject is not an ancestor")
    subject_tree = git(root, "rev-parse", f"{source_subject}^{{tree}}")
    if subject_tree != expected_source_tree:
        raise ValueError("protected source subject tree differs")
    return subject_tree


def validate_base_receipt(
    *,
    root: Path,
    receipt: dict[str, Any],
    head: str,
    tree: str,
) -> dict[str, Any]:
    if receipt.get("schema_version") != "hermes.bootstrap-closure-receipt/1.0":
        raise ValueError("base receipt schema differs")
    if receipt.get("emitter_repository") != {
        "full_name": REPOSITORY,
        "default_branch": "main",
    }:
        raise ValueError("base receipt repository differs")
    source = receipt.get("source")
    if not isinstance(source, dict) or source.get("head") != head or source.get("tree") != tree:
        raise ValueError("base receipt source differs from the exact checkout")
    current_lock_digest = sha256((root / "uv.lock").read_bytes())
    if source.get("lock_sha256") != current_lock_digest:
        raise ValueError("base receipt lock digest differs")
    if receipt.get("result_state") != "HERMES_BOOTSTRAP_CLOSURE_READY":
        raise ValueError("base receipt is not ready")
    if receipt.get("coverage") != "candidate_checkout":
        raise ValueError("base receipt coverage differs")
    if set(receipt.get("typed_omissions", [])) != BASE_TYPED_OMISSIONS:
        raise ValueError("base receipt typed omissions differ")

    manifest = receipt.get("manifest")
    validator = receipt.get("validator")
    for label, row, relative in (
        ("manifest", manifest, BASE_MANIFEST_REL.as_posix()),
        ("validator", validator, BASE_VALIDATOR_REL.as_posix()),
    ):
        if not isinstance(row, dict) or row.get("path") != relative:
            raise ValueError(f"base receipt {label} path differs")
        blob_sha, blob = head_blob(root, relative)
        if row.get("blob_sha") != blob_sha or row.get("sha256") != sha256(blob):
            raise ValueError(f"base receipt {label} provenance differs")

    dependencies = receipt.get("dependencies")
    if not isinstance(dependencies, list):
        raise ValueError("base receipt dependencies are missing")
    by_id: dict[str, dict[str, Any]] = {}
    for item in dependencies:
        if not isinstance(item, dict) or not isinstance(item.get("dependency_id"), str):
            raise ValueError("base receipt dependency is malformed")
        dependency_id = item["dependency_id"]
        if dependency_id in by_id:
            raise ValueError("base receipt dependency ID is duplicated")
        by_id[dependency_id] = item
    if set(by_id) != set(BASE_DEPENDENCIES):
        raise ValueError("base receipt dependency surface differs")
    for dependency_id, relative in BASE_DEPENDENCIES.items():
        item = by_id[dependency_id]
        blob_sha, blob = head_blob(root, relative)
        if (
            item.get("canonical_path_or_provider") != relative
            or item.get("dependency_class") != "ORG_NATIVE_BOOTSTRAP"
            or item.get("local_required") is not True
            or item.get("status") != "READY"
            or item.get("blob_sha") != blob_sha
            or item.get("digest") != sha256(blob)
        ):
            raise ValueError(f"base receipt dependency provenance differs: {dependency_id}")

    receipt_id = receipt.get("receipt_id")
    payload_digest = receipt.get("canonical_payload_digest")
    if not isinstance(receipt_id, str) or not RECEIPT_ID.fullmatch(receipt_id):
        raise ValueError("base receipt ID is invalid")
    if not isinstance(payload_digest, str) or not SHA64.fullmatch(payload_digest):
        raise ValueError("base receipt payload digest is invalid")
    payload = {
        key: value
        for key, value in receipt.items()
        if key not in {"receipt_id", "canonical_payload_digest"}
    }
    observed_digest = canonical_digest(payload)
    if observed_digest != payload_digest or receipt_id != f"hermes-bootstrap-{payload_digest[:20]}":
        raise ValueError("base receipt canonical digest differs")

    return {
        "receipt_id": receipt_id,
        "canonical_payload_digest": payload_digest,
        "source": {"head": head, "tree": tree, "lock_sha256": current_lock_digest},
        "manifest": {
            "path": manifest["path"],
            "blob_sha": manifest["blob_sha"],
            "sha256": manifest["sha256"],
        },
        "validator": {
            "path": validator["path"],
            "blob_sha": validator["blob_sha"],
            "sha256": validator["sha256"],
            "version": validator.get("version"),
        },
        "dependency_count": len(BASE_DEPENDENCIES),
        "result_state": receipt["result_state"],
        "typed_omissions": sorted(BASE_TYPED_OMISSIONS),
    }


def build_receipt(
    *,
    root: Path,
    environment: dict[str, str],
    proof_suite_result: str,
    base_receipt: dict[str, Any],
    proof_observation: ObservedProof | None = None,
    base_receipt_path: str = "artifacts/bootstrap/hermes-bootstrap-closure-receipt-v1.json",
    source_subject: str = SOURCE_SUBJECT,
    expected_source_tree: str = SOURCE_TREE,
) -> dict[str, Any]:
    root = root.resolve()
    head = git(root, "rev-parse", "HEAD")
    tree = git(root, "rev-parse", "HEAD^{tree}")

    run_id, run_attempt = validate_workflow_identity(
        environment=environment,
        head=head,
        proof_suite_result=proof_suite_result,
    )
    subject_tree = validate_source_ancestry(
        root=root,
        head=head,
        source_subject=source_subject,
        expected_source_tree=expected_source_tree,
    )
    base_closure = validate_base_receipt(
        root=root,
        receipt=base_receipt,
        head=head,
        tree=tree,
    )

    policy_bytes = (root / POLICY_REL).read_bytes()
    pin_text = (root / PIN_REL).read_text(encoding="utf-8")
    policy = json.loads(policy_bytes)
    validated = validate_policy(
        policy=policy,
        policy_bytes=policy_bytes,
        pin_text=pin_text,
    )
    stages: dict[str, Any] = {}
    for name in ("stage0", "stage1"):
        row = policy[name]
        observed = git(root, "rev-parse", f"HEAD:{row['path']}")
        if observed != row["blob_sha"]:
            raise ValueError(f"{name} blob differs from the protected policy")
        stages[name] = {
            "path": row["path"],
            "blob_sha": observed,
            "mode": git(root, "ls-tree", "HEAD", row["path"]).split()[0],
        }
    if proof_observation is None or set(proof_observation.claims) != set(PROOF_BINDINGS):
        raise ValueError("proof results do not cover the exact claim bindings")
    if proof_observation.result != "PASS" or set(proof_observation.claims.values()) != {"PASS"}:
        raise ValueError("one or more bound proof nodes did not pass")
    expected_base_path = PROOF_BINDINGS["base_v1_closure"].split(":", 1)[1]
    if base_receipt_path != expected_base_path:
        raise ValueError("base receipt path differs from its proof binding")

    return {
        "schema_version": "hermes.self-healing-bootstrap-receipt/3.0.0",
        "state": "HERMES_SELF_HEALING_BOOTSTRAP_V2_CANDIDATE",
        "canonical": False,
        "repository": REPOSITORY,
        "source_subject": {
            "commit": source_subject,
            "tree": subject_tree,
        },
        "source_under_test": {
            "commit": head,
            "tree": tree,
            "strict_descendant_or_subject": True,
        },
        "base_closure": base_closure,
        "policy": {
            "path": POLICY_REL.as_posix(),
            "sha256": validated["sha256"],
            "git_blob_sha": git(root, "rev-parse", f"HEAD:{POLICY_REL.as_posix()}"),
            "shared_home_authority_allowed": False,
            "fixed_operation": validated["operation"],
            "stages": stages,
        },
        "recovery_proof": {
            "result": proof_observation.result,
            **proof_observation.claims,
            "proof_bindings": dict(PROOF_BINDINGS),
            "proof_binding_semantics": "many claims may bind distinct assertions in one executed test node",
            "proof_junit_sha256": proof_observation.junit_sha256,
            "proof_suites": list(proof_observation.suites),
        },
        "evidence": {
            "workflow_name": WORKFLOW_NAME,
            "workflow_path": WORKFLOW_PATH,
            "event": environment["GITHUB_EVENT_NAME"],
            "run_id": run_id,
            "run_attempt": run_attempt,
            "job": environment.get("GITHUB_JOB", ""),
            "ref_name": environment.get("GITHUB_REF_NAME", ""),
            "sha": environment["GITHUB_SHA"],
            "artifact_name": "hermes-self-healing-bootstrap-v2-candidate",
        },
        "publication": {
            "candidate_artifact_only": True,
            "canonical_path": "evidence/bootstrap-closure/receipt-v1.json",
            "requires_protected_main_push": True,
            "requires_protected_commit_check_readback": True,
            "requires_exact_artifact_member_verification": True,
            "requires_strict_descendant_receipt_pr": True,
        },
        "non_claims": [
            "no shared-home environment authority",
            "no arbitrary package installation",
            "no provider or credential authority",
            "no release deployment or production authority",
            "no customer-data finance science brokerage capital or trading authority",
            "no branch-protection review or merge bypass",
            "candidate artifact is not the canonical product receipt",
            "protected-main check-run and exact archive-member verification remain publication gates",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".", type=Path)
    parser.add_argument("--base-receipt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--proof-junit", required=True, type=Path)
    args = parser.parse_args()
    base_receipt = json.loads(args.base_receipt.read_text(encoding="utf-8"))
    if not isinstance(base_receipt, dict):
        raise SystemExit("base receipt must be a JSON object")
    proof_observation = observed_proof(args.proof_junit)
    receipt = build_receipt(
        root=args.root,
        environment=dict(os.environ),
        proof_suite_result="PASS",
        base_receipt=base_receipt,
        proof_observation=proof_observation,
        base_receipt_path=args.base_receipt.as_posix(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
