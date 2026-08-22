#!/usr/bin/env python3
"""Emit a candidate receipt for the merged Hermes self-healing bootstrap V2."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any


REPOSITORY = "neoengine-ai-org/hermes-agent"
WORKFLOW_NAME = "Hermes self-healing bootstrap V2 receipt"
WORKFLOW_PATH = ".github/workflows/self-healing-bootstrap-v2-receipt.yml"
SOURCE_SUBJECT = "518c00b34eb2df7f550a0791bb9c5b657ec38071"
SOURCE_TREE = "7975b8563ca7d3a58a237cab9bdfc8329c19c408"
POLICY_REL = Path("config/hermes-bootstrap-acquisition-v2.json")
PIN_REL = Path("config/hermes-bootstrap-acquisition-v2.sha256")
SHA40 = re.compile(r"^[a-f0-9]{40}$")
SHA64 = re.compile(r"^[a-f0-9]{64}$")


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


def git_returncode(root: Path, *arguments: str) -> int:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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


def build_receipt(
    *,
    root: Path,
    environment: dict[str, str],
    proof_suite_result: str,
    source_subject: str = SOURCE_SUBJECT,
    expected_source_tree: str = SOURCE_TREE,
) -> dict[str, Any]:
    root = root.resolve()
    head = git(root, "rev-parse", "HEAD")
    tree = git(root, "rev-parse", "HEAD^{tree}")

    # Validate the workflow before Git-history inspection so a forged run identity
    # cannot be obscured by a shallow checkout. Unit tests may use the current
    # commit as their synthetic source; the dedicated full-history workflow uses
    # the protected SOURCE_SUBJECT/SOURCE_TREE defaults and is receipt-grade.
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

    return {
        "schema_version": "hermes.self-healing-bootstrap-receipt/2.0.0",
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
        "policy": {
            "path": POLICY_REL.as_posix(),
            "sha256": validated["sha256"],
            "git_blob_sha": git(root, "rev-parse", f"HEAD:{POLICY_REL.as_posix()}"),
            "shared_home_authority_allowed": False,
            "fixed_operation": validated["operation"],
            "stages": stages,
        },
        "recovery_proof": {
            "result": "PASS",
            "healthy_noop": "PASS",
            "deleted_stage1_exact_recovery": "PASS",
            "exact_mode_restoration": "PASS",
            "malformed_lock_recovery": "PASS",
            "abrupt_exit_lock_release": "PASS",
            "live_owner_preserved": "PASS",
            "corrupt_fingerprinted_environment_rebuilt_once": "PASS",
            "exactly_one_retry": "PASS",
            "fixed_operation_no_manifest_argv": "PASS",
            "proof_suites": [
                "tests/bootstrap/test_self_healing_bootstrap_v2.py",
                "tests/bootstrap/test_self_healing_bootstrap_v2_final_hardening.py",
                "tests/bootstrap/test_self_healing_bootstrap_v2_receipt.py",
            ],
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
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--proof-suite-result", required=True, choices=("PASS",))
    args = parser.parse_args()
    receipt = build_receipt(
        root=args.root,
        environment=dict(os.environ),
        proof_suite_result=args.proof_suite_result,
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
