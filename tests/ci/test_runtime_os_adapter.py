from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

# The first select_tests() call in this process AST-parses every repository
# source and test (~1.9k files, ~4s on a quiet machine). Under tests.yml's
# 8-worker slices on 2-vCPU hosted runners that one-time parse can approach
# the global 30s per-test cap, so this file gets a larger, still bounded cap.
pytestmark = pytest.mark.timeout(120)

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "_runtime_os_adapter_test", ROOT / "scripts/ci/runtime_os_adapter.py"
)
assert SPEC and SPEC.loader
adapter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adapter
SPEC.loader.exec_module(adapter)
PARITY_FIXTURES = json.loads(
    (ROOT / "ci/runtime-os/hermes-parity-fixtures.v1.json").read_text(encoding="utf-8")
)


def test_policy_lock_verifies_canonical_identity() -> None:
    policy = adapter.load_policy()
    assert policy["policy_version"] == "2.1.0"
    assert policy["source_commit"] == "871e416afc55db187d2b6f29c9ff7cac96472223"
    assert (
        adapter.EXPECTED_POLICY_DIGEST
        == "1bdb16a0322fb654b519b49e4608d6d9f369fa1572ac1901a596605262525b19"
    )
    assert (
        adapter.EXPECTED_PARITY_FIXTURE_DIGEST
        == "ed3f140b8324c746791173a084e4a6ea7bedb2e6e27c3eb9079cb5d194f708dd"
    )
    assert policy["stable_contexts"] == [
        "Hermes CI required",
        "Review evidence required",
        "Merge admission",
    ]
    assert policy["repository_profile"]["id"] == "hermes-agent"
    assert policy["canonical_decision_contract"]["types_digest"].startswith("sha256:")


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


def test_canonical_parity_and_historical_escape_fixtures() -> None:
    policy = adapter.load_policy()
    assert PARITY_FIXTURES["canonical_runtime_os"]["source_commit"] == policy["source_commit"]
    assert PARITY_FIXTURES["stable_contexts"] == policy["stable_contexts"]
    for fixture in PARITY_FIXTURES["fixtures"]:
        files = fixture["changed_files"]
        full, _ = adapter.full_proof(files, fixture["event_name"], policy)
        selected, unknown = adapter.select_tests(files)
        observed_full = full or unknown
        assert observed_full is fixture["expected_full_proof"], fixture["id"]
        if "expected_selected_tests" in fixture:
            assert set(fixture["expected_selected_tests"]).issubset(selected), fixture["id"]


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


def test_module_mapping_includes_direct_import_and_monkeypatch_consumers() -> None:
    selected, unknown = adapter.select_tests(["agent/rate_limit_tracker.py"])
    assert "tests/agent/test_rate_limit_tracker.py" in selected
    assert "tests/agent/test_nous_rate_guard.py" in selected
    assert "tests/gateway/test_usage_command.py" in selected
    assert unknown is False


def test_module_mapping_closes_transitive_source_to_test_dependencies() -> None:
    selected, unknown = adapter.select_tests(["agent/file_safety.py"])
    assert "tests/agent/test_file_safety_credentials.py" in selected
    assert "tests/tools/test_write_deny.py" in selected
    assert "tests/agent/test_copilot_acp_deprecation.py" in selected
    assert unknown is False


def test_non_test_python_helper_forces_full_proof() -> None:
    selected, unknown = adapter.select_tests(["tests/conftest.py"])
    assert selected == []
    assert unknown is True


def test_unknown_non_python_executable_forces_full_proof() -> None:
    assert adapter.select_tests(["scripts/novel-check.sh"]) == ([], True)
    assert adapter.select_tests(["runtime/novel.rs"]) == ([], True)
    assert adapter.select_tests(["runtime/novel.tsx"]) == ([], True)
    assert adapter.select_tests(["Dockerfile"]) == ([], True)
    assert adapter.select_tests(["locales/en.yaml"]) == ([], True)
    assert adapter.select_tests(["gateway/assets/status_phrases.yaml"]) == ([], True)
    assert adapter.select_tests(["scripts/Deploy.SH"]) == ([], True)
    assert adapter.select_tests(["db/Migration.SQL"]) == ([], True)


def test_nix_and_composite_actions_force_full_proof() -> None:
    policy = adapter.load_policy()
    for path in ("flake.nix", "flake.lock", "nix/devShell.nix", ".github/actions/retry/action.yml"):
        assert adapter.full_proof([path], "pull_request", policy)[0] is True


def test_plan_emits_complete_workflow_output_contract(tmp_path, monkeypatch) -> None:
    output = tmp_path / "github-output"
    body = tmp_path / "body.md"
    body.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    args = argparse.Namespace(
        changed_files_json='["tests/ci/test_runtime_os_adapter.py"]',
        event_name="pull_request",
        ref="refs/pull/81/merge",
        body_file=str(body),
        additions=1,
        pr_number="81",
        repo="neoengine-ai-org/hermes-agent",
    )
    assert adapter.plan(args) == 0
    keys = {line.split("=", 1)[0] for line in output.read_text(encoding="utf-8").splitlines()}
    assert keys == {
        "plan",
        "matrix",
        "risk_class",
        "review_route",
        "review_classification",
        "run_e2e",
        "has_tests",
        "telemetry_write_allowed",
    }


def test_python_source_discovery_skips_generated_environment_trees(monkeypatch, tmp_path) -> None:
    for relative in (
        "pkg/module.py",
        ".venv/lib/python3.11/site-packages/dep.py",
        ".bootstrap-proof-venv/lib/python3.11/site-packages/dep.py",
        "ci-fast/bin/.python/cpython-3.11.16-linux-x86_64-gnu/lib/python3.11/ast.py",
        "tests/test_module.py",
        "pkg/ci-fast/nested_source.py",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("import os\n", encoding="utf-8")
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    # Only the repository-root generated trees are skipped.
    assert adapter.discover_python_sources() == ["pkg/ci-fast/nested_source.py", "pkg/module.py"]


def _plan_args(tmp_path, files: list[str]):
    body = tmp_path / "body.md"
    body.write_text("", encoding="utf-8")
    return argparse.Namespace(
        changed_files_json=json.dumps(files),
        event_name="push",
        ref="refs/heads/main",
        body_file=str(body),
        additions=0,
        pr_number="unknown",
        repo="neoengine-ai-org/hermes-agent",
    )


def test_full_proof_with_zero_unit_tests_fails_closed(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(adapter, "discover_tests", lambda: [])
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    with pytest.raises(ValueError, match="zero unit test files"):
        adapter.plan(_plan_args(tmp_path, ["pyproject.toml"]))


def test_colon_in_selected_test_path_fails_closed(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        adapter, "discover_tests", lambda: ["tests/test_a.py:tests/test_b.py", "tests/test_c.py"]
    )
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    with pytest.raises(ValueError, match="cannot contain ':'"):
        adapter.plan(_plan_args(tmp_path, ["pyproject.toml"]))


def test_newline_in_selected_test_path_fails_closed(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(adapter, "discover_tests", lambda: ["tests/test_a.py\ntests/test_b.py"])
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    with pytest.raises(ValueError, match="control characters"):
        adapter.plan(_plan_args(tmp_path, ["pyproject.toml"]))


def _pairwise_fixpoint(changed_module: str) -> set[str]:
    # The original quadratic rescan, kept as an oracle for the indexed BFS.
    impacted = {changed_module}
    changed = True
    while changed:
        changed = False
        for source_path in adapter.discover_python_sources():
            module = adapter._module_name(source_path)
            if module in impacted:
                continue
            try:
                if any(adapter._imports_module(source_path, name) for name in impacted):
                    impacted.add(module)
                    changed = True
            except (OSError, SyntaxError, UnicodeError):
                pass
    return impacted


def test_indexed_closure_matches_pairwise_fixpoint(monkeypatch, tmp_path) -> None:
    files = {
        "pkg/a.py": "x = 1\n",
        "pkg/b.py": "from pkg import a\n",
        "pkg/c.py": "import pkg.b as b\n",
        "pkg/d.py": "TARGET = 'pkg.c.helper'\n",
        "pkg/e.py": "from pkg.f import g\n",
        "pkg/f.py": "from pkg import e\n",
        "pkg/g.py": "import pkgx\n",
        "pkg/broken.py": "def (:\n",
        "other/z.py": "from pkg.d import TARGET\n",
    }
    for relative, text in files.items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(text, encoding="utf-8")
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    adapter._module_references.cache_clear()
    adapter._reference_prefixes.cache_clear()
    for changed in ("pkg.a", "pkg.e", "pkg.g", "pkg", "pkgx", "other.z"):
        impacted, failures = adapter._impacted_closure(changed)
        assert impacted == _pairwise_fixpoint(changed), changed
        assert failures == ["pkg/broken.py"]
    assert adapter._impacted_closure("pkg.a")[0] == {"pkg.a", "pkg.b", "pkg.c", "pkg.d", "other.z"}
    adapter._module_references.cache_clear()
    adapter._reference_prefixes.cache_clear()


def test_discovery_tolerates_directories_vanishing_mid_walk(monkeypatch, tmp_path) -> None:
    # Parallel test processes create and delete tests/__pycache__ while the
    # adapter walks the tree; a vanished directory must be skipped, not raise.
    for relative in ("tests/test_kept.py", "tests/__pycache__/x.pyc", "pkg/mod.py", "pkg/__pycache__/y.pyc"):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text("", encoding="utf-8")
    real_scandir = os.scandir

    def vanishing_scandir(path="."):
        if Path(path).name == "__pycache__":
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real_scandir(path)

    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    monkeypatch.setattr(os, "scandir", vanishing_scandir)
    assert adapter.discover_tests() == ["tests/test_kept.py"]
    assert adapter.discover_python_sources() == ["pkg/mod.py"]
