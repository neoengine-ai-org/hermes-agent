from __future__ import annotations

import argparse
import errno
import importlib.util
import json
import os
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
    real_scandir = os.scandir
    forbidden = {".venv", ".git", "venv", "integration"}

    def guarded_scandir(path=".", *args, **kwargs):
        assert not (set(Path(os.fspath(path)).relative_to(tmp_path).parts) & forbidden), path
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", guarded_scandir)
    assert adapter.discover_python_sources() == ["agent/core.py"]
    assert adapter.discover_tests() == ["tests/test_core.py"]


def test_discovery_never_scans_bytecode_caches(tmp_path, monkeypatch) -> None:
    # Parallel test files share the checkout; ``hermes update`` tests and
    # fresh interpreters delete/recreate ``__pycache__`` concurrently, which
    # made Python 3.11 rglob raise FileNotFoundError on tests/__pycache__.
    _write_tree(tmp_path, ["agent/core.py", "tests/test_core.py"])
    (tmp_path / "tests/__pycache__").mkdir()
    (tmp_path / "agent/__pycache__").mkdir()
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    real_scandir = os.scandir

    def racing_scandir(path=".", *args, **kwargs):
        if Path(os.fspath(path)).name == "__pycache__":
            raise FileNotFoundError(2, "No such file or directory", os.fspath(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", racing_scandir)
    assert adapter.discover_python_sources() == ["agent/core.py"]
    assert adapter.discover_tests() == ["tests/test_core.py"]


def test_discovery_fails_closed_when_a_source_dir_vanishes(tmp_path, monkeypatch) -> None:
    _write_tree(tmp_path, ["agent/core.py", "gateway/run.py"])
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    real_scandir = os.scandir

    def vanishing_scandir(path=".", *args, **kwargs):
        if Path(os.fspath(path)).name == "gateway":
            raise FileNotFoundError(2, "No such file or directory", os.fspath(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", vanishing_scandir)
    with pytest.raises(FileNotFoundError):
        adapter.discover_python_sources()


@pytest.mark.parametrize("error", [errno.EIO, errno.ELOOP, errno.EACCES])
def test_discovery_fails_closed_when_root_stat_errors(tmp_path, monkeypatch, error) -> None:
    _write_tree(tmp_path, ["agent/core.py", "tests/test_core.py"])
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    real_stat = os.stat
    roots = {os.fspath(tmp_path), os.fspath(tmp_path / "tests")}

    def failing_stat(path, *args, **kwargs):
        if os.fspath(path) in roots:
            raise OSError(error, os.strerror(error), os.fspath(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", failing_stat)
    with pytest.raises(OSError):
        adapter.discover_python_sources()
    with pytest.raises(OSError):
        adapter.discover_tests()


def test_discovery_of_missing_root_is_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    assert adapter.discover_tests() == []


def test_discovery_fails_closed_on_unreadable_dir(tmp_path, monkeypatch) -> None:
    _write_tree(tmp_path, ["agent/core.py", "locked/hidden.py"])
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    real_scandir = os.scandir

    def denied_scandir(path=".", *args, **kwargs):
        if Path(os.fspath(path)).name == "locked":
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", denied_scandir)
    with pytest.raises(PermissionError):
        adapter.discover_python_sources()


@pytest.mark.parametrize("error", [errno.EIO, errno.EACCES, errno.ELOOP])
def test_discovery_fails_closed_when_a_file_stat_errors(tmp_path, monkeypatch, error) -> None:
    _write_tree(tmp_path, ["agent/core.py", "agent/broken.py", "tests/test_broken.py"])
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    real_stat = os.stat

    def failing_stat(path, *args, **kwargs):
        if Path(os.fspath(path)).name in {"broken.py", "test_broken.py"}:
            raise OSError(error, os.strerror(error), os.fspath(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", failing_stat)
    with pytest.raises(OSError):
        adapter.discover_python_sources()
    with pytest.raises(OSError):
        adapter.discover_tests()


class _FailingDirEntry:
    def __init__(self, entry, error):
        self._entry = entry
        self._error = error
        self.name = entry.name
        self.path = entry.path

    def is_dir(self, *, follow_symlinks=True):
        raise OSError(self._error, os.strerror(self._error), self.path)


class _ScannerWithFailingEntry:
    def __init__(self, scanner, name, error):
        self._scanner = scanner
        self._name = name
        self._error = error

    def __enter__(self):
        self._scanner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._scanner.__exit__(*exc)

    def __iter__(self):
        for entry in self._scanner:
            yield _FailingDirEntry(entry, self._error) if entry.name == self._name else entry


@pytest.mark.parametrize("error", [errno.EACCES, errno.EIO])
def test_discovery_fails_closed_when_entry_classification_errors(
    tmp_path, monkeypatch, error
) -> None:
    # os.walk swallows DirEntry.is_dir() errors and treats the entry as a
    # file, which silently dropped the whole subtree from selection.
    _write_tree(tmp_path, ["agent/core.py", "gateway/run.py", "tests/unit/test_run.py"])
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    real_scandir = os.scandir
    cases = (("gateway", adapter.discover_python_sources), ("unit", adapter.discover_tests))
    for name, discover in cases:

        def failing_scandir(path=".", *args, _name=name, **kwargs):
            return _ScannerWithFailingEntry(real_scandir(path, *args, **kwargs), _name, error)

        monkeypatch.setattr(os, "scandir", failing_scandir)
        with pytest.raises(OSError):
            discover()


def test_discovery_does_not_follow_directory_symlinks(tmp_path, monkeypatch) -> None:
    _write_tree(tmp_path, ["agent/core.py", "elsewhere/linked.py"])
    (tmp_path / "agent/linked_dir").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    (tmp_path / "agent/linked_file.py").symlink_to(tmp_path / "agent/core.py")
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    assert adapter.discover_python_sources() == [
        "agent/core.py",
        "agent/linked_file.py",
        "elsewhere/linked.py",
    ]


def test_discovery_skips_files_that_vanish_or_dangle(tmp_path, monkeypatch) -> None:
    _write_tree(tmp_path, ["agent/core.py"])
    (tmp_path / "agent/dangling.py").symlink_to(tmp_path / "missing.py")
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", tmp_path)
    assert adapter.discover_python_sources() == ["agent/core.py"]


def test_prefix_index_matches_reference_import_semantics() -> None:
    sources = adapter.discover_python_sources()[:200] + adapter.discover_tests()[:200]
    modules = sorted({adapter._module_name(path) for path in sources})
    for path in sources:
        try:
            adapter._module_references(path)
        except (OSError, SyntaxError, UnicodeError):
            continue
        for module in modules:
            assert adapter._imports_any_module(path, {module}) is adapter._imports_module(
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
