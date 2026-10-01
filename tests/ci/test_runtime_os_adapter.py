from __future__ import annotations

import argparse
import errno
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest


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


def _build_repository_reference_index() -> None:
    """Parse the real repository once, after collection and before any test.

    The whole-repo import closure must parse every source and test file
    (~36 MB of Python). ``_module_references`` is cached per process, so the
    parse already happens once; ``tests/ci/conftest.py`` builds it from
    ``pytest_collection_finish`` (only when tests from this module are
    selected, never for ``--collect-only``) so that one-time cost sits under
    the runner's per-file guard instead of being charged to whichever test
    runs first under the 30 s per-test hang guard. Errors are
    not cached, so files that fail to read/parse are re-raised to
    ``select_tests`` exactly as before.
    """
    for path in adapter.discover_python_sources() + adapter.discover_tests():
        try:
            adapter._module_references(path)
        except (OSError, SyntaxError, UnicodeError):
            pass



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
    for changed in ("pkg.a", "pkg.e", "pkg.g", "pkg", "pkgx", "other.z"):
        impacted, failures = adapter._impacted_closure(changed)
        assert impacted == _pairwise_fixpoint(changed), changed
        assert failures == ["pkg/broken.py"]
    assert adapter._impacted_closure("pkg.a")[0] == {"pkg.a", "pkg.b", "pkg.c", "pkg.d", "other.z"}


def test_discovery_tolerates_directories_vanishing_mid_walk(monkeypatch, tmp_path) -> None:
    # Parallel test processes create and delete tests/__pycache__ while the
    # adapter walks the tree. A bytecode cache vanishing mid-walk restarts the
    # walk, which then sees the tree without it. Any other vanished directory
    # fails closed (test_discovery_fails_closed_when_a_source_dir_vanishes).
    for relative in ("tests/test_kept.py", "tests/__pycache__/x.pyc", "pkg/mod.py", "pkg/__pycache__/y.pyc"):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text("", encoding="utf-8")

    def vanished(path, dir_fd):
        if Path(path).name == "__pycache__" and dir_fd is not None:
            cache = next(tmp_path.rglob("__pycache__"), None)
            if cache is not None:
                shutil.rmtree(cache)
            return FileNotFoundError(2, "No such file or directory", path)
        return None

    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    _fail_directory_open(monkeypatch, vanished)
    assert adapter.discover_tests() == ["tests/test_kept.py"]
    assert adapter.discover_python_sources() == ["pkg/mod.py"]



def test_discovery_still_raises_on_non_vanish_errors(monkeypatch, tmp_path) -> None:
    (tmp_path / "tests/locked").mkdir(parents=True)
    (tmp_path / "tests/test_a.py").write_text("", encoding="utf-8")

    def denied(path, dir_fd):
        if Path(path).name == "locked":
            return PermissionError(13, "Permission denied", path)
        return None

    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    _fail_directory_open(monkeypatch, denied)
    with pytest.raises(PermissionError):
        adapter.discover_tests()


def _fail_directory_open(monkeypatch, fail) -> None:
    """Inject errors where the walker opens each directory (``os.open``)."""
    real_open = os.open

    def failing_open(path, flags, *args, dir_fd=None, **kwargs):
        error = fail(os.fspath(path), dir_fd)
        if error is not None:
            raise error
        return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(os, "open", failing_open)


def _fail_entry_lstat(monkeypatch, fail) -> None:
    """Inject errors where the walker classifies each entry (``os.lstat``)."""
    real_lstat = os.lstat

    def failing_lstat(path, *args, **kwargs):
        error = fail(Path(os.fspath(path)).name)
        if error is not None:
            raise error
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", failing_lstat)


def _write_tree(root: Path, files: list[str]) -> None:
    for relative in files:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("import agent.core\n", encoding="utf-8")


def test_discovery_prunes_excluded_dirs_without_descending(tmp_path, monkeypatch) -> None:
    _write_tree(
        tmp_path,
        [
            "agent/core.py",
            ".venv/lib/site-packages/pkg/mod.py",
            ".git/hooks/hook.py",
            "nested/venv/lib/x.py",
            "tests/test_core.py",
            "tests/integration/test_live.py",
        ],
    )
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    forbidden = {".venv", ".git", "venv", "integration"}

    def never_opened(path, dir_fd):
        assert Path(path).name not in forbidden, path
        return None

    _fail_directory_open(monkeypatch, never_opened)
    assert adapter.discover_python_sources() == ["agent/core.py"]
    assert adapter.discover_tests() == ["tests/test_core.py"]


def test_discovery_fails_closed_when_a_source_dir_vanishes(tmp_path, monkeypatch) -> None:
    _write_tree(tmp_path, ["agent/core.py", "gateway/run.py"])
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)

    def vanished(path, dir_fd):
        if Path(path).name == "gateway":
            return FileNotFoundError(2, "No such file or directory", path)
        return None

    _fail_directory_open(monkeypatch, vanished)
    with pytest.raises(FileNotFoundError):
        adapter.discover_python_sources()


@pytest.mark.parametrize("error", [errno.EIO, errno.ELOOP, errno.EACCES])
def test_discovery_fails_closed_when_root_stat_errors(tmp_path, monkeypatch, error) -> None:
    _write_tree(tmp_path, ["agent/core.py", "tests/test_core.py"])
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)

    def failing_root(path, dir_fd):
        if dir_fd is not None and Path(path).name in {tmp_path.name, "tests"}:
            return OSError(error, os.strerror(error), path)
        return None

    _fail_directory_open(monkeypatch, failing_root)
    with pytest.raises(OSError):
        adapter.discover_python_sources()
    with pytest.raises(OSError):
        adapter.discover_tests()


def test_discovery_of_missing_root_is_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    assert adapter.discover_tests() == []


@pytest.mark.parametrize("swapped", ["agent", "tests"])
def test_parse_never_follows_a_directory_swapped_after_discovery(tmp_path, monkeypatch, swapped) -> None:
    # Discovery returns paths; a directory swapped for a symlink to a decoy
    # afterwards must make the read fail closed, not parse the decoy.
    _write_tree(tmp_path, ["agent/core.py", "tests/test_core.py"])
    (tmp_path / "decoy").mkdir()
    (tmp_path / "decoy/core.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "decoy/test_core.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    discovered = adapter.discover_python_sources() + adapter.discover_tests()
    assert discovered == ["agent/core.py", "decoy/core.py", "decoy/test_core.py", "tests/test_core.py"]
    (tmp_path / swapped).rename(tmp_path / f"{swapped}.moved")
    (tmp_path / swapped).symlink_to(tmp_path / "decoy", target_is_directory=True)
    target = "agent/core.py" if swapped == "agent" else "tests/test_core.py"
    with pytest.raises(ValueError, match="symlink"):
        adapter._module_references(target)


def test_parse_refuses_a_file_replaced_after_discovery(tmp_path, monkeypatch) -> None:
    _write_tree(tmp_path, ["agent/victim.py", "agent/decoy.py"])
    (tmp_path / "agent/decoy.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    assert adapter.discover_python_sources() == ["agent/decoy.py", "agent/victim.py"]
    # Same path, different file: in-place replacement after discovery.
    os.replace(tmp_path / "agent/decoy.py", tmp_path / "agent/victim.py")
    with pytest.raises(ValueError, match="changed after discovery"):
        adapter._module_references("agent/victim.py")


def test_parse_refuses_a_file_swapped_for_a_symlink(tmp_path, monkeypatch) -> None:
    _write_tree(tmp_path, ["agent/victim.py"])
    (tmp_path / "decoy.txt").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    assert adapter.discover_python_sources() == ["agent/victim.py"]
    (tmp_path / "agent/victim.py").unlink()
    (tmp_path / "agent/victim.py").symlink_to(tmp_path / "decoy.txt")
    with pytest.raises(ValueError, match="symlink"):
        adapter._module_references("agent/victim.py")


def test_parse_cache_is_keyed_by_discovered_file_identity(tmp_path, monkeypatch) -> None:
    _write_tree(tmp_path, ["agent/victim.py"])
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    adapter.discover_python_sources()
    assert "agent.core" in adapter._module_references("agent/victim.py")
    replacement = tmp_path / "replacement.py"
    replacement.write_text("import gateway.run\n", encoding="utf-8")
    os.replace(replacement, tmp_path / "agent/victim.py")
    adapter.discover_python_sources()  # re-discovery records the new identity
    references = adapter._module_references("agent/victim.py")
    assert "gateway.run" in references and "agent.core" not in references


def test_candidate_read_matches_read_text(tmp_path) -> None:
    (tmp_path / "pkg").mkdir()
    payload = "import agent.core\r\nNAME = 'tools.registry'\r\n# caf\u00e9\n"
    (tmp_path / "pkg/mod.py").write_bytes(payload.encode("utf-8"))
    assert adapter._read_candidate(tmp_path, "pkg/mod.py") == (tmp_path / "pkg/mod.py").read_text(encoding="utf-8")


def test_discovery_fails_closed_when_root_is_not_a_directory(tmp_path, monkeypatch) -> None:
    (tmp_path / "tests").write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    with pytest.raises(NotADirectoryError):
        adapter.discover_tests()


@pytest.mark.parametrize("target", ["tests", "agent"])
def test_discovery_never_traverses_a_directory_swapped_for_a_symlink(
    tmp_path, monkeypatch, target
) -> None:
    # Swap a classified directory for a symlink to a decoy between the check
    # and its use: the descriptor-based walk must refuse it, not follow it.
    _write_tree(tmp_path, ["agent/core.py", "tests/test_core.py", "decoy/test_only.py", "decoy/evil.py"])
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    real_open = os.open
    swapped = []

    def swapping_open(path, flags, *args, dir_fd=None, **kwargs):
        if os.fspath(path).endswith(target) and not swapped:
            victim = tmp_path / target
            victim.rename(tmp_path / f"{target}.moved")
            victim.symlink_to(tmp_path / "decoy", target_is_directory=True)
            swapped.append(victim)
        return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(os, "open", swapping_open)
    discover = adapter.discover_tests if target == "tests" else adapter.discover_python_sources
    with pytest.raises(ValueError, match="symlink"):
        discover()
    assert swapped


@pytest.mark.parametrize("error", [errno.EIO, errno.EACCES, errno.ELOOP])
def test_discovery_fails_closed_when_a_file_stat_errors(tmp_path, monkeypatch, error) -> None:
    _write_tree(tmp_path, ["agent/core.py", "agent/broken.py", "tests/test_broken.py"])
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    real_lstat = os.lstat

    def failing_lstat(path, *args, **kwargs):
        if Path(os.fspath(path)).name in {"broken.py", "test_broken.py"}:
            raise OSError(error, os.strerror(error), os.fspath(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", failing_lstat)
    with pytest.raises(OSError):
        adapter.discover_python_sources()
    with pytest.raises(OSError):
        adapter.discover_tests()


@pytest.mark.parametrize("error", [errno.EACCES, errno.EIO])
def test_discovery_fails_closed_when_entry_classification_errors(
    tmp_path, monkeypatch, error
) -> None:
    # os.walk swallowed classification errors and treated the entry as a
    # file, silently dropping the whole subtree from selection.
    _write_tree(tmp_path, ["agent/core.py", "gateway/run.py", "tests/unit/test_run.py"])
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    for name, discover in (("gateway", adapter.discover_python_sources), ("unit", adapter.discover_tests)):
        _fail_entry_lstat(
            monkeypatch,
            lambda entry, _name=name: OSError(error, os.strerror(error), entry) if entry == _name else None,
        )
        with pytest.raises(OSError):
            discover()


def test_discovery_does_not_follow_directory_symlinks(tmp_path, monkeypatch) -> None:
    # A directory symlink is never traversed; because pytest would collect
    # through it, discovery refuses it rather than silently omitting it.
    _write_tree(tmp_path, ["agent/core.py", "elsewhere/linked.py"])
    (tmp_path / "agent/linked_dir").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        adapter.discover_python_sources()


@pytest.mark.parametrize("target", ["inside", "dangling", "escaping"])
def test_discovery_refuses_python_file_symlinks(tmp_path, monkeypatch, target) -> None:
    # A *.py symlink can dangle in the planner's checkout layout yet resolve
    # in the execution layout (or escape the tree); neither following nor
    # skipping it is safe, so discovery refuses it.
    root = tmp_path / "candidate"
    _write_tree(root, ["agent/core.py", "tests/test_kept.py"])
    (tmp_path / "outside.py").write_text("import agent.core\n", encoding="utf-8")
    destination = {
        "inside": root / "agent/core.py",
        "dangling": root / "missing.py",
        "escaping": tmp_path / "outside.py",
    }[target]
    (root / "tests/test_layout.py").symlink_to(destination)
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", root)
    with pytest.raises(ValueError, match="symlink"):
        adapter.discover_tests()


def test_discovery_fails_closed_when_a_listed_file_vanishes(tmp_path, monkeypatch) -> None:
    _write_tree(tmp_path, ["agent/core.py", "agent/gone.py", "tests/test_gone.py"])
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    real_stat, real_lstat = os.stat, os.lstat

    def vanished(path):
        return Path(os.fspath(path)).name in {"gone.py", "test_gone.py"}

    def stat_gone(path, *args, **kwargs):
        if vanished(path):
            raise FileNotFoundError(2, "No such file or directory", os.fspath(path))
        return real_stat(path, *args, **kwargs)

    def lstat_gone(path, *args, **kwargs):
        if vanished(path):
            raise FileNotFoundError(2, "No such file or directory", os.fspath(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat_gone)
    monkeypatch.setattr(os, "lstat", lstat_gone)
    with pytest.raises(FileNotFoundError):
        adapter.discover_python_sources()
    with pytest.raises(FileNotFoundError):
        adapter.discover_tests()


def test_discovery_fails_closed_when_a_listed_entry_vanishes_before_classification(
    tmp_path, monkeypatch
) -> None:
    # A source subtree removed between listing and classification must not
    # silently drop out of selection.
    _write_tree(tmp_path, ["agent/core.py", "gateway/run.py", "tests/unit/test_run.py"])
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    for name, discover in (("gateway", adapter.discover_python_sources), ("unit", adapter.discover_tests)):
        _fail_entry_lstat(
            monkeypatch,
            lambda entry, _name=name: FileNotFoundError(2, "No such file or directory", entry)
            if entry == _name
            else None,
        )
        with pytest.raises(FileNotFoundError):
            discover()


def test_discovery_includes_tracked_python_inside_bytecode_caches(tmp_path, monkeypatch) -> None:
    # __pycache__ is still walked: a tracked *.py hidden in one must not drop
    # out of selection (only its disappearance mid-walk is tolerated).
    _write_tree(
        tmp_path,
        ["agent/core.py", "agent/__pycache__/hidden.py", "tests/__pycache__/test_hidden.py"],
    )
    (tmp_path / "agent/__pycache__/core.cpython-311.pyc").write_bytes(b"")
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    assert adapter.discover_python_sources() == ["agent/__pycache__/hidden.py", "agent/core.py"]
    assert adapter.discover_tests() == ["tests/__pycache__/test_hidden.py"]


def test_discovery_tolerates_bytecode_cache_entries_vanishing(tmp_path, monkeypatch) -> None:
    # A cache entry that vanishes once restarts the walk; the restarted walk
    # still reports what is on disk (here: everything), never a narrowed set.
    _write_tree(tmp_path, ["agent/core.py", "agent/__pycache__/stale.py", "tests/test_core.py"])
    (tmp_path / "tests/__pycache__").mkdir()
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    raced: list[str] = []

    def once(entry):
        if entry in {"__pycache__", "stale.py"} and entry not in raced:
            raced.append(entry)
            return FileNotFoundError(2, "No such file or directory", entry)
        return None

    _fail_entry_lstat(monkeypatch, once)
    assert adapter.discover_python_sources() == ["agent/__pycache__/stale.py", "agent/core.py"]
    assert adapter.discover_tests() == ["tests/test_core.py"]
    assert raced


def test_reference_cache_is_keyed_by_candidate_root(tmp_path, monkeypatch) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    for root, target in ((first, "agent.alpha"), (second, "agent.beta")):
        (root / "pkg").mkdir(parents=True)
        (root / "pkg/mod.py").write_text(f"import {target}\n", encoding="utf-8")
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", first)
    assert "agent.alpha" in adapter._module_references("pkg/mod.py")
    assert "agent.alpha" in adapter._reference_prefixes("pkg/mod.py")
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", second)
    assert "agent.beta" in adapter._module_references("pkg/mod.py")
    assert "agent.alpha" not in adapter._module_references("pkg/mod.py")
    prefixes = adapter._reference_prefixes("pkg/mod.py")
    assert "agent.beta" in prefixes and "agent.alpha" not in prefixes


@pytest.mark.parametrize("root", ["tests", "."])
def test_discovery_refuses_a_symlinked_root(tmp_path, monkeypatch, root) -> None:
    # tests -> decoy would let a candidate replace the full-proof test set.
    real = tmp_path / "real"
    _write_tree(real, ["agent/core.py", "decoy/test_only.py"])
    if root == "tests":
        (real / "tests").symlink_to(real / "decoy", target_is_directory=True)
        monkeypatch.setattr(adapter, "CANDIDATE_ROOT", real)
        with pytest.raises(ValueError, match="symlink"):
            adapter.discover_tests()
    else:
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        monkeypatch.setattr(adapter, "CANDIDATE_ROOT", link)
        with pytest.raises(ValueError, match="symlink"):
            adapter.discover_python_sources()


def test_collection_hook_builds_index_only_when_adapter_tests_run() -> None:
    import importlib.util as _util
    from types import SimpleNamespace

    spec = _util.spec_from_file_location("_tests_ci_conftest", ROOT / "tests/ci/conftest.py")
    assert spec and spec.loader
    hook_module = _util.module_from_spec(spec)
    spec.loader.exec_module(hook_module)
    calls: list[str] = []

    class _Module:
        def _build_repository_reference_index(self) -> None:
            calls.append("built")

    adapter_item = SimpleNamespace(module=_Module(), path=Path("tests/ci/test_runtime_os_adapter.py"))
    adapter_item_2 = SimpleNamespace(module=adapter_item.module, path=adapter_item.path)
    other_item = SimpleNamespace(module=_Module(), path=Path("tests/ci/test_other.py"))

    def session(items, collect_only=False):
        return SimpleNamespace(config=SimpleNamespace(option=SimpleNamespace(collectonly=collect_only)), items=items)

    hook_module.pytest_collection_finish(session([adapter_item, adapter_item_2], collect_only=True))
    assert calls == []
    hook_module.pytest_collection_finish(session([other_item]))
    assert calls == []
    hook_module.pytest_collection_finish(session([other_item, adapter_item, adapter_item_2]))
    assert calls == ["built"]


def test_prefix_index_matches_reference_import_semantics() -> None:
    sources = adapter.discover_python_sources()[:200] + adapter.discover_tests()[:200]
    modules = sorted({adapter._module_name(path) for path in sources})
    with adapter._plan_snapshot():
        for path in sources:
            try:
                adapter._module_references(path)
            except (OSError, SyntaxError, UnicodeError):
                continue
            for module in modules:
                assert (module in adapter._reference_prefixes(path)) is adapter._imports_module(
                    path, module
                ), (path, module)


def test_node_iteration_visits_exactly_the_ast_walk_node_set() -> None:
    import ast

    paths = [
        "scripts/ci/runtime_os_adapter.py",
        "scripts/ci_risk_classifier.py",
        "tests/ci/test_runtime_os_adapter.py",
    ]
    sources = [(ROOT / relative).read_text(encoding="utf-8") for relative in paths]
    sources.append(
        "match event:\n"
        "    case {'kind': 'agent.core', **rest} if rest:\n"
        "        import agent.core as core\n"
        "    case [first, *others] | (first, *others):\n"
        "        from gateway import run\n"
        "    case Point(x=0, y=y) as point:\n"
        "        value = f'{point!r:>{y}} tools.registry'\n"
        "    case _:\n"
        "        pass\n"
        "async def go(xs):\n"
        "    async with ctx() as c:\n"
        "        return [y async for y in xs if (z := y)] + [lambda *a, k=1, **kw: a]\n"
    )
    for source in sources:
        tree = ast.parse(source)
        walked = sorted(map(id, ast.walk(tree)))
        assert sorted(map(id, adapter._iter_nodes(tree))) == walked


# --- Round-6 (f1a86bdc) blocking predicates -------------------------------


def _select_tree(tmp_path, monkeypatch, files: dict[str, str]) -> Path:
    root = tmp_path / "candidate"
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", root)
    return root


def test_p1_same_inode_rewrite_after_warm_selection_is_reselected(tmp_path, monkeypatch) -> None:
    root = _select_tree(
        tmp_path,
        monkeypatch,
        {
            "pkg/__init__.py": "",
            "pkg/old.py": "",
            "pkg/new.py": "",
            "tests/unit/test_probe.py": "import pkg.old\n",
            "tests/unit/test_new.py": "import pkg.unrelated\n",
        },
    )
    probe = root / "tests/unit/test_probe.py"
    inode = probe.stat().st_ino
    assert adapter.select_tests(["pkg/old.py"]) == (["tests/unit/test_probe.py"], False)
    probe.write_text("import pkg.new\n", encoding="utf-8")  # same inode, new content
    assert probe.stat().st_ino == inode
    selected, unknown = adapter.select_tests(["pkg/new.py"])
    assert "tests/unit/test_probe.py" in selected
    assert unknown is False


def test_p1_atomic_replacement_after_warm_selection_is_reselected(tmp_path, monkeypatch) -> None:
    root = _select_tree(
        tmp_path,
        monkeypatch,
        {
            "pkg/__init__.py": "",
            "pkg/old.py": "",
            "pkg/new.py": "",
            "tests/unit/test_probe.py": "import pkg.old\n",
            "tests/unit/test_new.py": "import pkg.unrelated\n",
        },
    )
    assert adapter.select_tests(["pkg/old.py"]) == (["tests/unit/test_probe.py"], False)
    replacement = root / "replacement.tmp"
    replacement.write_text("import pkg.new\n", encoding="utf-8")
    os.replace(replacement, root / "tests/unit/test_probe.py")
    selected, _ = adapter.select_tests(["pkg/new.py"])
    assert "tests/unit/test_probe.py" in selected


def test_p2_cache_resident_test_survives_transient_cache_churn(tmp_path, monkeypatch) -> None:
    # A tracked-style test inside tests/__pycache__ whose classification races
    # cache churn once must still be in the universe (the walk restarts).
    root = _select_tree(
        tmp_path,
        monkeypatch,
        {
            "pkg/__init__.py": "",
            "pkg/changed.py": "",
            "tests/unit/test_other.py": "import pkg.unrelated\n",
            "tests/__pycache__/test_cached.py": "import pkg.changed\n",
        },
    )
    real_lstat = os.lstat
    raced = []

    def churn_once(path, *args, **kwargs):
        if os.fspath(path) == "test_cached.py" and not raced:
            raced.append(path)
            raise FileNotFoundError(2, "No such file or directory", os.fspath(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", churn_once)
    selected, unknown = adapter.select_tests(["pkg/changed.py"])
    assert raced
    assert "tests/__pycache__/test_cached.py" in selected
    assert unknown is False
    assert (root / "tests/__pycache__/test_cached.py").exists()


def test_p2_persistent_cache_churn_fails_closed(tmp_path, monkeypatch) -> None:
    _select_tree(
        tmp_path,
        monkeypatch,
        {"tests/unit/test_other.py": "", "tests/__pycache__/test_cached.py": ""},
    )
    real_lstat = os.lstat

    def always_churning(path, *args, **kwargs):
        if os.fspath(path) == "test_cached.py":
            raise FileNotFoundError(2, "No such file or directory", os.fspath(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", always_churning)
    with pytest.raises(RuntimeError, match="bytecode cache"):
        adapter.discover_tests()


def test_p2_vanished_cache_directory_never_narrows_silently(tmp_path, monkeypatch) -> None:
    # The cache directory itself disappears between listing and opening: the
    # walk restarts and returns the universe as it now exists on disk.
    root = _select_tree(
        tmp_path,
        monkeypatch,
        {"tests/test_kept.py": "", "tests/__pycache__/test_cached.py": ""},
    )
    real_open = os.open
    removed = []

    def delete_cache_on_open(path, flags, *args, dir_fd=None, **kwargs):
        if os.fspath(path) == "__pycache__" and not removed:
            shutil.rmtree(root / "tests/__pycache__")
            removed.append(path)
        return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(os, "open", delete_cache_on_open)
    assert adapter.discover_tests() == ["tests/test_kept.py"]
    assert removed


@pytest.mark.parametrize("pruned", ["integration", "e2e", "docker"])
def test_p3_direct_changed_test_symlink_is_refused(tmp_path, monkeypatch, pruned) -> None:
    root = _select_tree(tmp_path, monkeypatch, {"tests/unit/test_kept.py": "", "decoy/test_decoy.py": ""})
    (root / f"tests/{pruned}").mkdir(parents=True)
    (root / f"tests/{pruned}/test_linked.py").symlink_to(root / "decoy/test_decoy.py")
    with pytest.raises(ValueError, match="symlink"):
        adapter.select_tests([f"tests/{pruned}/test_linked.py"])


def test_p3_direct_changed_test_through_symlinked_directory_is_refused(tmp_path, monkeypatch) -> None:
    root = _select_tree(tmp_path, monkeypatch, {"tests/unit/test_kept.py": "", "decoy/test_x.py": ""})
    (root / "tests/e2e").symlink_to(root / "decoy", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        adapter.select_tests(["tests/e2e/test_x.py"])


def test_p4_static_test_directory_symlink_fails_closed(tmp_path, monkeypatch) -> None:
    # pytest collects through directory symlinks, so a symlinked test
    # directory must not be silently outside the adapter's universe.
    root = _select_tree(
        tmp_path,
        monkeypatch,
        {
            "pkg/__init__.py": "",
            "pkg/linkmod.py": "",
            "tests/unit/test_linkmod.py": "import pkg.unrelated\n",
            "outside/test_linked.py": "import pkg.linkmod\n",
        },
    )
    (root / "tests/unit/linked").symlink_to(root / "outside", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        adapter.discover_tests()
    with pytest.raises(ValueError, match="symlink"):
        adapter.select_tests(["pkg/linkmod.py"])


def test_p4_source_directory_symlink_fails_closed(tmp_path, monkeypatch) -> None:
    root = _select_tree(tmp_path, monkeypatch, {"agent/core.py": "", "elsewhere/mod.py": ""})
    (root / "agent/linked").symlink_to(root / "elsewhere", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        adapter.discover_python_sources()


def test_p5_child_directory_swapped_between_classification_and_open(tmp_path, monkeypatch) -> None:
    root = _select_tree(
        tmp_path,
        monkeypatch,
        {"tests/unit/test_a.py": "", "tests/unit/test_b.py": "", "tests/test_top.py": ""},
    )
    real_open = os.open
    swapped = []

    def swap_before_open(path, flags, *args, dir_fd=None, **kwargs):
        if os.fspath(path) == "unit" and dir_fd is not None and not swapped:
            os.rename(root / "tests/unit", root / "unit.moved")
            (root / "tests/unit").mkdir()  # different, empty, real directory
            swapped.append(path)
        return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_open)
    with pytest.raises(ValueError, match="changed during discovery"):
        adapter.discover_tests()
    assert swapped


def test_p5_child_directory_swapped_between_listing_and_classification(tmp_path, monkeypatch) -> None:
    root = _select_tree(
        tmp_path,
        monkeypatch,
        {"tests/unit/test_a.py": "", "tests/test_top.py": ""},
    )
    real_lstat = os.lstat
    swapped = []

    def swap_before_lstat(path, *args, **kwargs):
        if os.fspath(path) == "unit" and not swapped:
            os.rename(root / "tests/unit", root / "unit.moved")
            (root / "tests/unit").mkdir()
            swapped.append(path)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", swap_before_lstat)
    with pytest.raises(ValueError, match="changed during discovery"):
        adapter.discover_tests()
    assert swapped


@pytest.mark.parametrize("decoy_kind", ["symlink", "real_directory"])
def test_p6_candidate_root_ancestor_swap_is_refused(tmp_path, monkeypatch, decoy_kind) -> None:
    anchor = tmp_path / "anchor"
    root = anchor / "candidate"
    _write_tree(root, ["agent/core.py", "tests/test_core.py"])
    decoy_anchor = tmp_path / "decoy_anchor"
    _write_tree(decoy_anchor / "candidate", ["agent/other.py", "tests/test_decoy.py"])
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", root)
    assert adapter.discover_tests() == ["tests/test_core.py"]  # binds the root
    os.rename(anchor, tmp_path / "anchor.moved")
    if decoy_kind == "symlink":
        anchor.symlink_to(decoy_anchor, target_is_directory=True)
        match = "symlink"
    else:
        os.rename(decoy_anchor, anchor)
        match = "candidate root changed"
    with pytest.raises(ValueError, match=match):
        adapter.discover_tests()
    with pytest.raises(ValueError, match=match):
        adapter._module_references("agent/core.py")


def test_p7_backslash_path_is_never_aliased_to_a_slash_path(tmp_path, monkeypatch) -> None:
    root = _select_tree(tmp_path, monkeypatch, {"tests/unit/test_a.py": ""})
    (root / "tests/unit\\test_a.py").write_text("", encoding="utf-8")  # distinct legal POSIX name
    selected, unknown = adapter.select_tests(["tests/unit\\test_a.py"])
    assert "tests/unit/test_a.py" not in selected
    assert unknown is True
