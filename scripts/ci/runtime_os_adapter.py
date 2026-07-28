#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import functools
import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

TRUST_ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_ROOT = Path(os.environ.get("RUNTIME_OS_CANDIDATE_ROOT", TRUST_ROOT)).resolve()
LOCK_PATH = TRUST_ROOT / "ci/runtime-os/policy-bundle.lock.json"
EXPECTED_POLICY_VERSION = "2.1.0"
EXPECTED_SOURCE_COMMIT = "871e416afc55db187d2b6f29c9ff7cac96472223"
EXPECTED_POLICY_DIGEST = "1bdb16a0322fb654b519b49e4608d6d9f369fa1572ac1901a596605262525b19"
EXPECTED_PARITY_FIXTURE_DIGEST = "ed3f140b8324c746791173a084e4a6ea7bedb2e6e27c3eb9079cb5d194f708dd"
EXPECTED_CONTEXTS = ["Hermes CI required", "Review evidence required", "Merge admission"]
TEST_SUFFIXES = {".py"}
EXECUTABLE_NAMES = {"Dockerfile", "Makefile"}


def load_policy() -> dict[str, Any]:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    bundle_path = TRUST_ROOT / lock["bundle"]
    payload = bundle_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if lock["digest"] != EXPECTED_POLICY_DIGEST or digest != EXPECTED_POLICY_DIGEST:
        raise ValueError(f"policy digest mismatch: expected {EXPECTED_POLICY_DIGEST}, got {digest}")
    policy = json.loads(payload)
    required = {
        "policy_version": EXPECTED_POLICY_VERSION,
        "source_commit": EXPECTED_SOURCE_COMMIT,
        "stable_contexts": EXPECTED_CONTEXTS,
        "slice_count": 6,
        "mode": "advisory",
        "candidate_may_rewrite_policy": False,
        "private_cross_repo_checkout": False,
        "telemetry_source": "protected_main_only",
        "canonical_decision_contract": {
            "proof_selection_digest": "sha256:fb41392b598faa4ab442f305e4a6dff61811d53cd5046a388219d09de4d41e2b",
            "proof_selection_test_digest": "sha256:3a2b7bd064f161821773aac1ef2eedcdf2afde7215f3ce51df585fe7427211d9",
            "review_routing_digest": "sha256:5a9220bb1c5b741f0607ec33213c0632db25533608d1ee1bf8b19eefb7ce92a5",
            "review_routing_test_digest": "sha256:18a1ac74525f8a622dbd3144cb7bac8262c4d6c6f271ed5c84b89b54309a9c8a",
            "types_digest": "sha256:cb76164215f07a4e64e0cc442adf5b6c44deec93d6b3c796ec33164f113dc71b",
        },
        "repository_profile": {
            "capability_classes": [
                "deterministic_unit",
                "integration_e2e",
                "review_evidence",
                "merge_admission",
            ],
            "full_proof_events": [
                "merge_group",
                "push",
                "schedule",
                "workflow_dispatch",
            ],
            "id": "hermes-agent",
            "narrow_selection": "transitive_python_import_closure",
            "parity_fixtures": "ci/runtime-os/hermes-parity-fixtures.v1.json",
            "unknown_impact": "full_proof",
            "version": 1,
        },
    }
    for key, expected in required.items():
        if policy.get(key) != expected:
            raise ValueError(f"invalid policy field {key}: expected {expected!r}")
    fixture_path = TRUST_ROOT / policy["repository_profile"]["parity_fixtures"]
    fixture_digest = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    if fixture_digest != EXPECTED_PARITY_FIXTURE_DIGEST:
        raise ValueError("repository parity fixture digest mismatch")
    if lock["policy_version"] != EXPECTED_POLICY_VERSION or lock["source_commit"] != EXPECTED_SOURCE_COMMIT:
        raise ValueError("policy lock identity mismatch")
    return policy


def load_classifier() -> Any:
    path = TRUST_ROOT / "scripts/ci_risk_classifier.py"
    spec = importlib.util.spec_from_file_location("_runtime_os_trusted_classifier", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load trusted classifier")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.SELF_CHANGE_PATHS.add("scripts/ci/runtime_os_adapter.py")
    return module


def full_proof(files: list[str], event_name: str, policy: dict[str, Any]) -> tuple[bool, str]:
    if event_name in set(policy["repository_profile"]["full_proof_events"]):
        return True, f"{event_name}_requires_full_proof"
    triggers = policy["full_proof_triggers"]
    for path in files:
        normalized = path.replace("\\", "/").removeprefix("./")
        if any(normalized == trigger or normalized.startswith(trigger) for trigger in triggers):
            return True, f"full_proof_trigger:{normalized}"
    return False, "narrow_change"


def discover_tests() -> list[str]:
    skip_parts = {"integration", "e2e", "docker"}
    return sorted(
        str(path.relative_to(CANDIDATE_ROOT))
        for path in (CANDIDATE_ROOT / "tests").rglob("test_*.py")
        if path.is_file() and not (set(path.relative_to(CANDIDATE_ROOT).parts) & skip_parts)
    )


def discover_python_sources() -> list[str]:
    excluded = {".git", ".venv", "tests", "venv"}
    return sorted(
        str(path.relative_to(CANDIDATE_ROOT))
        for path in CANDIDATE_ROOT.rglob("*.py")
        if path.is_file()
        and not (set(path.relative_to(CANDIDATE_ROOT).parts) & excluded)
    )


def _module_name(path: str) -> str:
    module = path.removesuffix(".py").replace("/", ".")
    return module.removesuffix(".__init__")


@functools.lru_cache(maxsize=None)
def _module_references(path: str) -> frozenset[str]:
    source = (CANDIDATE_ROOT / path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=path)
    references: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            references.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported = node.module or ""
            if imported:
                references.add(imported)
                references.update(
                    f"{imported}.{alias.name}"
                    for alias in node.names
                    if alias.name != "*"
                )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            references.update(
                re.findall(
                    r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b",
                    node.value,
                )
            )
    return frozenset(references)


def _imports_module(path: str, module_name: str) -> bool:
    """Return whether a source/test directly references a module boundary."""
    return any(
        reference == module_name or reference.startswith(f"{module_name}.")
        for reference in _module_references(path)
    )


def select_tests(files: list[str]) -> tuple[list[str], bool]:
    all_tests = discover_tests()
    classifier = load_classifier()
    executable_suffixes = set(classifier.EXECUTABLE_SUFFIXES)
    selected: set[str] = set()
    unknown_executable = False
    for raw in files:
        path = raw.replace("\\", "/").removeprefix("./")
        candidate = CANDIDATE_ROOT / path
        if path.startswith("tests/") and candidate.suffix in TEST_SUFFIXES and candidate.name.startswith("test_") and candidate.exists():
            selected.add(path)
            continue
        suffix = candidate.suffix.lower()
        if suffix not in executable_suffixes and candidate.name not in EXECUTABLE_NAMES:
            if not classifier.is_documentation_file(path):
                unknown_executable = True
            continue
        if suffix == ".py" and not path.startswith("tests/"):
            stem = candidate.stem.removeprefix("test_")
            impacted_modules = {_module_name(path)}
            source_paths = discover_python_sources()
            changed = True
            while changed:
                changed = False
                for source_path in source_paths:
                    source_module = _module_name(source_path)
                    if source_module in impacted_modules:
                        continue
                    try:
                        if any(
                            _imports_module(source_path, module)
                            for module in impacted_modules
                        ):
                            impacted_modules.add(source_module)
                            changed = True
                    except (OSError, SyntaxError, UnicodeError):
                        unknown_executable = True
            matches: list[str] = []
            for test in all_tests:
                if Path(test).stem == f"test_{stem}":
                    matches.append(test)
                    continue
                try:
                    if any(
                        _imports_module(test, module)
                        for module in impacted_modules
                    ):
                        matches.append(test)
                except (OSError, SyntaxError, UnicodeError):
                    unknown_executable = True
            if matches:
                selected.update(matches)
                continue
        unknown_executable = True
    return sorted(selected), unknown_executable


def slice_matrix(files: list[str], count: int = 6) -> dict[str, list[dict[str, object]]]:
    durations_path = Path(os.environ.get("RUNTIME_OS_DURATIONS_PATH", CANDIDATE_ROOT / "test_durations.json"))
    durations = json.loads(durations_path.read_text(encoding="utf-8")) if durations_path.exists() else {}
    bucket_count = min(count, len(files))
    buckets: list[list[str]] = [[] for _ in range(bucket_count)]
    totals = [0.0] * bucket_count

    def duration(path: str) -> float:
        try:
            return max(0.0, float(durations.get(path, 1.0)))
        except (TypeError, ValueError):
            return 1.0

    weighted = sorted(files, key=lambda path: (-duration(path), path))
    for path in weighted:
        index = min(range(bucket_count), key=lambda item: (totals[item], item))
        buckets[index].append(path)
        totals[index] += duration(path)
    return {"include": [{"index": index + 1, "files": ":".join(bucket)} for index, bucket in enumerate(buckets) if bucket]}


def write_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def build_review_classification(classification: Any) -> dict[str, object]:
    review_classification = classification.as_dict()
    if classification.risk_class in {"R0", "R1", "R2"}:
        review_classification.update(
            required_reviews=[],
            secondary_review_required=False,
            adversarial_review_required=False,
            opposite_provider_required=False,
            opposite_frontier_required=False,
            human_gate_required=False,
            founder_review_required=False,
            model_tier_required=0,
        )
    elif classification.risk_class == "R3":
        review_classification.update(
            required_reviews=["adversarial_review_required"],
            secondary_review_required=False,
            adversarial_review_required=True,
            opposite_provider_required=False,
            opposite_frontier_required=False,
            human_gate_required=False,
            founder_review_required=False,
            model_tier_required=0,
        )
    return review_classification


def plan(args: argparse.Namespace) -> int:
    policy = load_policy()
    files = json.loads(args.changed_files_json)
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise ValueError("changed files must be a JSON string array")
    body = Path(args.body_file).read_text(encoding="utf-8") if args.body_file else ""
    classification = load_classifier().classify(files, body, additions=args.additions, pr_number=args.pr_number, repo=args.repo)
    review_classification = build_review_classification(classification)
    run_full, reason = full_proof(files, args.event_name, policy)
    selected, unknown = select_tests(files)
    if unknown:
        run_full, reason = True, "unknown_executable_fails_closed"
    tests = discover_tests() if run_full else selected
    matrix = slice_matrix(tests)
    review_key = "R4-R5" if classification.risk_class in {"R4", "R5"} else "R3" if classification.risk_class == "R3" else "R0-R2"
    result = {
        "policy_version": policy["policy_version"],
        "policy_source_commit": policy["source_commit"],
        "risk_class": classification.risk_class,
        "complexity_class": classification.complexity_class,
        "review_route": policy["review_model"][review_key],
        "full_proof": run_full,
        "reason": reason,
        "run_e2e": run_full,
        "matrix": matrix,
        "selected_test_count": len(tests),
        "telemetry_write_allowed": args.event_name == "push" and args.ref == "refs/heads/main",
    }
    encoded = json.dumps(result, separators=(",", ":"), sort_keys=True)
    print(json.dumps(result, indent=2, sort_keys=True))
    write_output("plan", encoded)
    write_output("matrix", json.dumps(matrix, separators=(",", ":")))
    write_output("risk_class", classification.risk_class)
    write_output("review_route", result["review_route"])
    write_output("review_classification", json.dumps(review_classification, separators=(",", ":"), sort_keys=True))
    write_output("run_e2e", str(run_full).lower())
    write_output("has_tests", str(bool(matrix["include"])).lower())
    write_output("telemetry_write_allowed", str(result["telemetry_write_allowed"]).lower())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--changed-files-json", required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--ref", default="")
    parser.add_argument("--body-file")
    parser.add_argument("--additions", type=int, default=0)
    parser.add_argument("--pr-number", default="unknown")
    parser.add_argument("--repo", default="unknown")
    return plan(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
