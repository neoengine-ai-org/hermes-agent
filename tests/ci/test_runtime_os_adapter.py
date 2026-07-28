from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "_runtime_os_adapter_test", ROOT / "scripts/ci/runtime_os_adapter.py"
)
assert SPEC and SPEC.loader
adapter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adapter
SPEC.loader.exec_module(adapter)


def test_policy_lock_verifies_canonical_identity() -> None:
    policy = adapter.load_policy()
    assert policy["policy_version"] == "2.1.0"
    assert policy["source_commit"] == "871e416afc55db187d2b6f29c9ff7cac96472223"
    assert (
        adapter.EXPECTED_POLICY_DIGEST
        == "14bf24d96f4705b9356394bfc1922d11280ef8f2aa3b5981611384a1a244852d"
    )
    assert policy["stable_contexts"] == [
        "Hermes CI required",
        "Review evidence required",
        "Merge admission",
    ]


def test_classifier_change_requires_full_six_slice_proof() -> None:
    full, reason = adapter.full_proof(
        ["scripts/ci_risk_classifier.py"], "pull_request", adapter.load_policy()
    )
    assert full is True
    assert reason.startswith("full_proof_trigger:")


def test_main_and_nightly_require_full_proof() -> None:
    policy = adapter.load_policy()
    assert adapter.full_proof(["README.md"], "push", policy)[0] is True
    assert adapter.full_proof(["README.md"], "schedule", policy)[0] is True


def test_direct_test_selection_is_narrow_and_nonempty() -> None:
    selected, unknown = adapter.select_tests(["tests/ci/test_runtime_os_adapter.py"])
    assert selected == ["tests/ci/test_runtime_os_adapter.py"]
    assert unknown is False
    assert adapter.slice_matrix(selected) == {
        "include": [{"index": 1, "files": "tests/ci/test_runtime_os_adapter.py"}]
    }


def test_unknown_executable_fails_closed() -> None:
    selected, unknown = adapter.select_tests(["new_package/novel_runtime.py"])
    assert selected == []
    assert unknown is True


def test_adapter_self_change_is_not_r0() -> None:
    classification = adapter.load_classifier().classify(
        ["scripts/ci/runtime_os_adapter.py"], ""
    )
    assert classification.risk_class == "R3"
    review = adapter.build_review_classification(classification)
    assert review["required_reviews"] == ["adversarial_review_required"]
    assert review["adversarial_review_required"] is True
    assert review["opposite_frontier_required"] is False


def test_docs_only_change_does_not_manufacture_empty_test_job() -> None:
    selected, unknown = adapter.select_tests(["docs/guide.md"])
    assert selected == []
    assert unknown is False
    assert adapter.slice_matrix(selected) == {"include": []}


def test_discovery_excludes_integration_e2e_and_docker() -> None:
    tests = adapter.discover_tests()
    assert not any(
        set(Path(test).parts) & {"integration", "e2e", "docker"} for test in tests
    )


def test_module_mapping_is_anchored_not_substring_based() -> None:
    selected, unknown = adapter.select_tests(["hermes/e.py"])
    assert selected == []
    assert unknown is True


def test_non_test_python_helper_forces_full_proof() -> None:
    selected, unknown = adapter.select_tests(["tests/conftest.py"])
    assert selected == []
    assert unknown is True


def test_unknown_non_python_executable_forces_full_proof() -> None:
    assert adapter.select_tests(["scripts/novel-check.sh"]) == ([], True)
    assert adapter.select_tests(["Dockerfile"]) == ([], True)
