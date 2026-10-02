from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
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
    (~36 MB of Python). Parse results are cached by git blob id, so the parse
    happens once per process; ``tests/ci/conftest.py`` builds it lazily, as
    the outermost ``pytest_runtest_protocol`` wrapper of the first test from
    this module that the process runs (never for ``--collect-only``, nor in
    an xdist worker scheduled none of them), so that one-time cost sits
    under the runner's per-file guard instead of being charged to whichever
    test runs first under the 30 s per-test hang guard. Errors are not cached, so
    files that fail to parse are re-raised to ``select_tests`` as before.
    """
    with adapter._plan_snapshot():  # one pinned commit: resolve HEAD once
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


# --- Candidate fixtures: real git repositories ------------------------------
#
# The adapter plans from the immutable git objects of the commit checked out at
# RUNTIME_OS_CANDIDATE_ROOT, so fixtures are committed temporary repositories.

_GIT_FIXTURE_ENV = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


def _git(root: Path, *args: str) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(_GIT_FIXTURE_ENV)
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=environment
    ).stdout.strip()


def _commit(
    root: Path,
    files: dict[str, str] | list[str],
    symlinks: dict[str, str] | None = None,
    message: str = "fixture",
) -> str:
    """Write ``files`` (and ``symlinks``: path -> target) into ``root`` and commit."""
    if not (root / ".git").exists():
        root.mkdir(parents=True, exist_ok=True)
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "core.autocrlf", "false")
    if isinstance(files, list):
        files = {relative: "import agent.core\n" for relative in files}
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    for relative, destination in (symlinks or {}).items():
        link = root / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(destination)
    _git(root, "add", "-A", "-f")
    _git(root, "commit", "-q", "--allow-empty", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _candidate(tmp_path, monkeypatch, files, symlinks=None) -> Path:
    root = (tmp_path / "candidate").resolve()
    _commit(root, files, symlinks)
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", root)
    return root


def test_python_source_discovery_skips_generated_environment_trees(monkeypatch, tmp_path) -> None:
    _candidate(
        tmp_path,
        monkeypatch,
        {
            relative: "import os\n"
            for relative in (
                "pkg/module.py",
                ".venv/lib/python3.11/site-packages/dep.py",
                ".bootstrap-proof-venv/lib/python3.11/site-packages/dep.py",
                "ci-fast/bin/.python/cpython-3.11.16-linux-x86_64-gnu/lib/python3.11/ast.py",
                "tests/test_module.py",
                "pkg/ci-fast/nested_source.py",
            )
        },
    )
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
    _candidate(
        tmp_path,
        monkeypatch,
        {
            "pkg/a.py": "x = 1\n",
            "pkg/b.py": "from pkg import a\n",
            "pkg/c.py": "import pkg.b as b\n",
            "pkg/d.py": "TARGET = 'pkg.c.helper'\n",
            "pkg/e.py": "from pkg.f import g\n",
            "pkg/f.py": "from pkg import e\n",
            "pkg/g.py": "import pkgx\n",
            "pkg/broken.py": "def (:\n",
            "other/z.py": "from pkg.d import TARGET\n",
        },
    )
    with adapter._plan_snapshot():
        for changed in ("pkg.a", "pkg.e", "pkg.g", "pkg", "pkgx", "other.z"):
            impacted, failures = adapter._impacted_closure(changed)
            assert impacted == _pairwise_fixpoint(changed), changed
            assert failures == ["pkg/broken.py"]
        assert adapter._impacted_closure("pkg.a")[0] == {"pkg.a", "pkg.b", "pkg.c", "pkg.d", "other.z"}


def test_discovery_ignores_bytecode_caches_and_untracked_files(monkeypatch, tmp_path) -> None:
    # Parallel test processes create and delete __pycache__ in the shared
    # checkout. Discovery reads the candidate commit's git tree, so neither
    # bytecode churn nor any untracked file can change (or break) the universe.
    root = _candidate(tmp_path, monkeypatch, {"tests/test_kept.py": "", "pkg/mod.py": ""})
    for relative in ("tests/__pycache__/x.pyc", "pkg/__pycache__/y.pyc", "tests/test_untracked.py", "pkg/untracked.py"):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text("import pkg.mod\n", encoding="utf-8")
    assert adapter.discover_tests() == ["tests/test_kept.py"]
    assert adapter.discover_python_sources() == ["pkg/mod.py"]
    shutil.rmtree(root / "tests/__pycache__")
    shutil.rmtree(root / "tests")  # even the working tree vanishing does not matter
    assert adapter.discover_tests() == ["tests/test_kept.py"]


def test_discovery_still_raises_on_git_errors(monkeypatch, tmp_path) -> None:
    # A candidate root that is not a git checkout (or whose objects cannot be
    # read) fails closed instead of producing an empty universe.
    plain = (tmp_path / "plain").resolve()
    (plain / "tests").mkdir(parents=True)
    (plain / "tests/test_a.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", plain)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.resolve()))
    with pytest.raises((RuntimeError, ValueError)):
        adapter.discover_tests()


def test_discovery_requires_the_repository_top_level(monkeypatch, tmp_path) -> None:
    root = _candidate(tmp_path, monkeypatch, {"tests/test_a.py": "", "sub/tests/test_b.py": ""})
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", root / "sub")
    with pytest.raises(ValueError, match="top level"):
        adapter.discover_tests()


def test_discovery_prunes_excluded_dirs(monkeypatch, tmp_path) -> None:
    _candidate(
        tmp_path,
        monkeypatch,
        [
            "agent/core.py",
            ".venv/lib/site-packages/pkg/mod.py",
            "nested/venv/lib/x.py",
            "tests/test_core.py",
            "tests/integration/test_live.py",
            "tests/e2e/sub/test_e2e.py",
        ],
    )
    assert adapter.discover_python_sources() == ["agent/core.py"]
    assert adapter.discover_tests() == ["tests/test_core.py"]


def test_discovery_of_a_missing_tests_tree_fails_closed(monkeypatch, tmp_path) -> None:
    # A commit without tests/ must not collapse discovery to an empty universe.
    _candidate(tmp_path, monkeypatch, {"agent/core.py": ""})
    with pytest.raises(FileNotFoundError):
        adapter.discover_tests()


def test_discovery_of_a_tests_entry_that_is_not_a_directory_fails_closed(monkeypatch, tmp_path) -> None:
    _candidate(tmp_path, monkeypatch, {"agent/core.py": "", "tests": "not a directory"})
    with pytest.raises(NotADirectoryError):
        adapter.discover_tests()


def test_discovery_includes_tracked_python_inside_bytecode_caches(monkeypatch, tmp_path) -> None:
    _candidate(
        tmp_path,
        monkeypatch,
        {"agent/core.py": "", "agent/__pycache__/hidden.py": "", "tests/__pycache__/test_hidden.py": ""},
    )
    assert adapter.discover_python_sources() == ["agent/__pycache__/hidden.py", "agent/core.py"]
    assert adapter.discover_tests() == ["tests/__pycache__/test_hidden.py"]


@pytest.mark.parametrize(
    "link, target",
    [
        ("tests/unit/linked", "../../outside"),  # directory link pytest collects through
        ("tests/unit/test_linked.py", "../../outside/test_linked.py"),  # Python file link
        ("tests/unit/dangling", "../../not-yet-there"),  # target could appear later
        ("tests/unit/receipt.md", "../../outside/notes.md"),  # even an inert-looking link
        ("tests/e2e", "../outside"),  # a link named like a pruned directory
        ("agent/linked", "../outside"),  # source-tree directory link
    ],
)
def test_discovery_refuses_committed_symlinks(monkeypatch, tmp_path, link, target) -> None:
    # A symlink can resolve differently in another checkout layout, and pytest
    # collects through directory links: never follow, never skip -- refuse.
    _candidate(
        tmp_path,
        monkeypatch,
        {"agent/core.py": "", "tests/unit/test_kept.py": "", "outside/test_linked.py": "import agent.core\n", "outside/notes.md": ""},
        symlinks={link: target},
    )
    discover = adapter.discover_python_sources if link.startswith("agent/") else adapter.discover_tests
    with pytest.raises(ValueError, match="symlink"):
        discover()


def test_discovery_refuses_a_symlinked_tests_root(monkeypatch, tmp_path) -> None:
    _candidate(tmp_path, monkeypatch, {"agent/core.py": "", "decoy/test_only.py": ""}, symlinks={"tests": "decoy"})
    with pytest.raises(ValueError, match="symlink"):
        adapter.discover_tests()


def test_discovery_refuses_a_symlinked_candidate_root(monkeypatch, tmp_path) -> None:
    real = _candidate(tmp_path, monkeypatch, {"agent/core.py": "", "tests/test_a.py": ""})
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", link)
    with pytest.raises(ValueError, match="symlink"):
        adapter.discover_tests()


def test_discovery_refuses_submodules(monkeypatch, tmp_path) -> None:
    root = _candidate(tmp_path, monkeypatch, {"agent/core.py": "", "tests/test_a.py": ""})
    commit = _git(root, "rev-parse", "HEAD")
    _git(root, "update-index", "--add", "--cacheinfo", f"160000,{commit},tests/vendored")
    _git(root, "commit", "-q", "-m", "gitlink")
    with pytest.raises(ValueError, match="submodule"):
        adapter.discover_tests()


def test_candidate_source_matches_read_text(monkeypatch, tmp_path) -> None:
    payload = "import agent.core\r\nNAME = 'tools.registry'\r\n# caf\u00e9\n"
    root = _candidate(tmp_path, monkeypatch, {"pkg/mod.py": payload})
    assert adapter._candidate_source("pkg/mod.py") == (root / "pkg/mod.py").read_text(encoding="utf-8")


def test_reference_cache_is_content_addressed_per_candidate_root(monkeypatch, tmp_path) -> None:
    first, second = (tmp_path / "first").resolve(), (tmp_path / "second").resolve()
    _commit(first, {"pkg/mod.py": "import agent.alpha\n"})
    _commit(second, {"pkg/mod.py": "import agent.beta\n"})
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", first)
    assert "agent.alpha" in adapter._module_references("pkg/mod.py")
    assert "agent.alpha" in adapter._reference_prefixes("pkg/mod.py")
    monkeypatch.setattr(adapter, "CANDIDATE_ROOT", second)
    assert "agent.beta" in adapter._module_references("pkg/mod.py")
    assert "agent.alpha" not in adapter._module_references("pkg/mod.py")
    prefixes = adapter._reference_prefixes("pkg/mod.py")
    assert "agent.beta" in prefixes and "agent.alpha" not in prefixes


def _load_ci_conftest():
    spec = importlib.util.spec_from_file_location("_tests_ci_conftest", ROOT / "tests/ci/conftest.py")
    assert spec and spec.loader
    hook_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook_module)
    return hook_module


def test_runtest_hook_builds_index_once_and_only_for_adapter_tests() -> None:
    from types import SimpleNamespace

    hook_module = _load_ci_conftest()
    calls: list[str] = []

    class _Module:
        def _build_repository_reference_index(self) -> None:
            calls.append("built")

    adapter_module = _Module()
    adapter_item = SimpleNamespace(module=adapter_module, path=Path("tests/ci/test_runtime_os_adapter.py"))
    other_item = SimpleNamespace(module=_Module(), path=Path("tests/ci/test_other.py"))

    def run(item) -> None:
        wrapper = hook_module.pytest_runtest_protocol(item, None)
        next(wrapper)  # everything before the yield runs before inner wrappers
        with pytest.raises(StopIteration):
            wrapper.send(True)

    run(other_item)
    assert calls == []
    run(adapter_item)
    run(SimpleNamespace(module=adapter_module, path=adapter_item.path))
    assert calls == ["built"]


_HOOK_PROBE_TEST = """
import os, time
from pathlib import Path

def _build_repository_reference_index():
    with Path(os.environ["INDEX_BUILD_LOG"]).open("a", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()}\\n")
    time.sleep(1.5)

def test_adapter_one():
    pass

def test_adapter_two():
    pass
"""


def _run_hook_probe(tmp_path, *extra: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run pytest on a probe tree using the real tests/ci/conftest.py hook.

    The probe's index build sleeps 1.5 s under a 0.5 s per-test timeout, so a
    build charged to any test's timer fails that test.
    """
    probe = tmp_path / "probe"
    probe.mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "tests/ci/conftest.py", probe / "conftest.py")
    (probe / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (probe / "test_runtime_os_adapter.py").write_text(_HOOK_PROBE_TEST, encoding="utf-8")
    (probe / "test_other.py").write_text(
        "".join(f"def test_other_{index}():\n    pass\n\n" for index in range(8)), encoding="utf-8"
    )
    log = tmp_path / "builds.log"
    log.unlink(missing_ok=True)
    environment = {**os.environ, "INDEX_BUILD_LOG": str(log)}
    environment.pop("PYTEST_ADDOPTS", None)
    completed = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(probe / "pytest.ini"),
            "--rootdir", str(probe), "--timeout=0.5", "--timeout-method=signal", *extra, str(probe),
        ],
        capture_output=True,
        text=True,
        env=environment,
        cwd=probe,
        timeout=25,
        check=False,
    )
    builds = log.read_text(encoding="utf-8").split() if log.exists() else []
    return completed, builds


def test_index_build_runs_outside_the_per_test_timeout(tmp_path) -> None:
    pytest.importorskip("pytest_timeout")
    completed, builds = _run_hook_probe(tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert len(builds) == 1
    completed, builds = _run_hook_probe(tmp_path, "-k", "other")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert builds == []
    completed, builds = _run_hook_probe(tmp_path, "--collect-only")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert builds == []


def test_xdist_workers_without_adapter_tests_skip_the_index_build(tmp_path) -> None:
    pytest.importorskip("pytest_timeout")
    pytest.importorskip("xdist")
    # loadfile pins the adapter module to one worker; the others never build.
    completed, builds = _run_hook_probe(tmp_path, "-n", "3", "--dist", "loadfile")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert len(builds) == 1


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


# --- Exact-head review predicates (f1a86bdc round 6), on git candidates -------


def _probe_candidate(tmp_path, monkeypatch) -> Path:
    return _candidate(
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


def test_p1_content_change_after_warm_selection_is_reselected(tmp_path, monkeypatch) -> None:
    # Warm selection, then the same test file changes its import in a new
    # commit (same path): the parse cache is content-addressed by blob id.
    root = _probe_candidate(tmp_path, monkeypatch)
    assert adapter.select_tests(["pkg/old.py"]) == (["tests/unit/test_probe.py"], False)
    _commit(root, {"tests/unit/test_probe.py": "import pkg.new\n"})
    selected, unknown = adapter.select_tests(["pkg/new.py"])
    assert "tests/unit/test_probe.py" in selected
    assert unknown is False


def test_p1_working_tree_edits_never_leak_into_a_plan(tmp_path, monkeypatch) -> None:
    # The plan reads the committed blob; an uncommitted in-place rewrite of the
    # same file (same inode) cannot be served or mixed in.
    root = _probe_candidate(tmp_path, monkeypatch)
    probe = root / "tests/unit/test_probe.py"
    inode = probe.stat().st_ino
    with open(probe, "r+", encoding="utf-8") as handle:
        handle.write("import pkg.new\n")
    assert probe.stat().st_ino == inode
    assert adapter.select_tests(["pkg/old.py"]) == (["tests/unit/test_probe.py"], False)
    assert "tests/unit/test_probe.py" not in adapter.select_tests(["pkg/new.py"])[0]


def test_p1_plan_pins_one_commit_even_if_head_moves_mid_plan(tmp_path, monkeypatch) -> None:
    root = _probe_candidate(tmp_path, monkeypatch)
    real_closure = adapter._impacted_closure

    def closure_then_commit(module):
        result = real_closure(module)
        _commit(root, {"tests/unit/test_added.py": "import pkg.old\n"})
        return result

    monkeypatch.setattr(adapter, "_impacted_closure", closure_then_commit)
    # The plan saw one consistent commit (no torn view across HEAD moves).
    assert adapter.select_tests(["pkg/old.py"]) == (["tests/unit/test_probe.py"], False)
    monkeypatch.setattr(adapter, "_impacted_closure", real_closure)
    assert "tests/unit/test_added.py" in adapter.select_tests(["pkg/old.py"])[0]


def test_p1_plan_full_proof_discovery_uses_the_selection_commit(tmp_path, monkeypatch) -> None:
    # plan() selects, then (full proof) rediscovers tests; HEAD moving between
    # the two must not change the commit full-proof discovery reads.
    root = _probe_candidate(tmp_path, monkeypatch)
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    real_select, real_discover = adapter.select_tests, adapter.discover_tests
    selection_commit: list[str] = []
    discovery_commits: list[str] = []

    def select_then_commit(files):
        result = real_select(files)
        selection_commit.append(adapter._candidate_tree()[0])
        _commit(root, {"tests/unit/test_added.py": "import pkg.old\n"})
        return result

    def recording_discover():
        discovered = real_discover()
        discovery_commits.append(adapter._candidate_tree()[0])
        return discovered

    monkeypatch.setattr(adapter, "select_tests", select_then_commit)
    monkeypatch.setattr(adapter, "discover_tests", recording_discover)
    # A push to main forces full proof; selection still reads the tree first.
    assert adapter.plan(_plan_args(tmp_path, ["pkg/old.py"])) == 0
    plan_output = json.loads(output.read_text(encoding="utf-8").splitlines()[0].split("=", 1)[1])
    assert plan_output["full_proof"] is True
    # Selection's own discovery, then plan()'s full-proof discovery after HEAD moved.
    assert len(discovery_commits) == 2
    assert set(discovery_commits) == set(selection_commit)
    assert selection_commit[0] != _git(root, "rev-parse", "HEAD")
    planned = ":".join(entry["files"] for entry in plan_output["matrix"]["include"]).split(":")
    assert sorted(planned) == ["tests/unit/test_new.py", "tests/unit/test_probe.py"]
    # A fresh plan sees the moved HEAD.
    monkeypatch.setattr(adapter, "select_tests", real_select)
    monkeypatch.setattr(adapter, "discover_tests", real_discover)
    assert "tests/unit/test_added.py" in adapter.discover_tests()


def test_p2_tracked_test_inside_bytecode_cache_is_selected(tmp_path, monkeypatch) -> None:
    root = _candidate(
        tmp_path,
        monkeypatch,
        {
            "pkg/__init__.py": "",
            "pkg/changed.py": "",
            "tests/unit/test_other.py": "import pkg.unrelated\n",
            "tests/__pycache__/test_cached.py": "import pkg.changed\n",
        },
    )
    shutil.rmtree(root / "tests/__pycache__")  # cache churn in the working tree
    selected, unknown = adapter.select_tests(["pkg/changed.py"])
    assert "tests/__pycache__/test_cached.py" in selected
    assert unknown is False


@pytest.mark.parametrize("pruned", ["integration", "e2e", "docker"])
def test_p3_direct_changed_test_symlink_is_refused(tmp_path, monkeypatch, pruned) -> None:
    _candidate(
        tmp_path,
        monkeypatch,
        {"tests/unit/test_kept.py": "", "decoy/test_decoy.py": "", f"tests/{pruned}/test_real.py": ""},
        symlinks={f"tests/{pruned}/test_linked.py": "../../decoy/test_decoy.py"},
    )
    with pytest.raises(ValueError, match="symlink"):
        adapter.select_tests([f"tests/{pruned}/test_linked.py"])


def test_p3_direct_changed_test_through_symlinked_directory_is_refused(tmp_path, monkeypatch) -> None:
    _candidate(
        tmp_path,
        monkeypatch,
        {"tests/unit/test_kept.py": "", "decoy/test_x.py": ""},
        symlinks={"tests/e2e": "../decoy"},
    )
    with pytest.raises(ValueError, match="symlink"):
        adapter.select_tests(["tests/e2e/test_x.py"])


def test_p4_static_test_directory_symlink_fails_closed(tmp_path, monkeypatch) -> None:
    _candidate(
        tmp_path,
        monkeypatch,
        {
            "pkg/__init__.py": "",
            "pkg/linkmod.py": "",
            "tests/unit/test_linkmod.py": "import pkg.unrelated\n",
            "outside/test_linked.py": "import pkg.linkmod\n",
        },
        symlinks={"tests/unit/linked": "../../outside"},
    )
    with pytest.raises(ValueError, match="symlink"):
        adapter.discover_tests()
    with pytest.raises(ValueError, match="symlink"):
        adapter.select_tests(["pkg/linkmod.py"])


def test_p7_backslash_path_is_never_aliased_to_a_slash_path(tmp_path, monkeypatch) -> None:
    _candidate(tmp_path, monkeypatch, {"tests/unit/test_a.py": "", "tests/unit\\test_a.py": ""})
    selected, unknown = adapter.select_tests(["tests/unit\\test_a.py"])
    assert "tests/unit/test_a.py" not in selected
    assert unknown is True


# --- Git-tree round 1 (7dbb6483): links inside pruned directories ------------


def _add_gitlink(root: Path, path: str) -> None:
    commit = _git(root, "rev-parse", "HEAD")
    _git(root, "update-index", "--add", "--cacheinfo", f"160000,{commit},{path}")
    _git(root, "commit", "-q", "-m", "gitlink")


@pytest.mark.parametrize("pruned", ["e2e", "integration", "docker"])
def test_g1_gitlink_inside_a_pruned_test_directory_is_refused(tmp_path, monkeypatch, pruned) -> None:
    # The execution checkout does not materialize submodules, so a gitlinked
    # tree under a pruned test directory would silently lack its tests while
    # a full-proof plan succeeds. Pruning must not hide it.
    root = _candidate(tmp_path, monkeypatch, {"tests/test_a.py": "", f"tests/{pruned}/test_real.py": ""})
    _add_gitlink(root, f"tests/{pruned}/vendored")
    with pytest.raises(ValueError, match="submodule"):
        adapter.discover_tests()
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    with pytest.raises(ValueError, match="submodule"):
        adapter.plan(_plan_args(tmp_path, ["pyproject.toml"]))


@pytest.mark.parametrize("pruned", [".venv", "venv", "ci-fast"])
def test_g1_gitlink_inside_a_pruned_source_directory_is_refused(tmp_path, monkeypatch, pruned) -> None:
    root = _candidate(tmp_path, monkeypatch, {"agent/core.py": "", "tests/test_a.py": ""})
    _add_gitlink(root, f"{pruned}/vendored")
    with pytest.raises(ValueError, match="submodule"):
        adapter.discover_python_sources()


@pytest.mark.parametrize("pruned", ["e2e", "integration", "docker"])
def test_g1_symlink_inside_a_pruned_test_directory_is_refused(tmp_path, monkeypatch, pruned) -> None:
    # e2e/integration/docker suites are collected by their own jobs; a link
    # there resolves differently per checkout, so it is refused like any
    # other test-tree link.
    _candidate(
        tmp_path,
        monkeypatch,
        {"tests/test_a.py": "", f"tests/{pruned}/test_real.py": "", "outside/test_x.py": ""},
        symlinks={f"tests/{pruned}/linked": "../../outside"},
    )
    with pytest.raises(ValueError, match="symlink"):
        adapter.discover_tests()


def test_g1_symlinks_inside_pruned_source_environments_are_ignored(tmp_path, monkeypatch) -> None:
    # A committed virtualenv legitimately contains symlinks (bin/python);
    # pruned source environments are never parsed, so their links are inert.
    _candidate(
        tmp_path,
        monkeypatch,
        {"agent/core.py": "", "tests/test_a.py": "", ".venv/lib/site.py": ""},
        symlinks={".venv/bin/python": "/usr/bin/python3"},
    )
    assert adapter.discover_python_sources() == ["agent/core.py"]


def test_g1_blob_batch_only_fetches_the_discovered_universe(tmp_path, monkeypatch) -> None:
    _candidate(
        tmp_path,
        monkeypatch,
        {"agent/core.py": "import os\n", "tests/test_a.py": "", ".venv/lib/big.py": "x = 1\n" * 1000},
    )
    fetched: list[str] = []
    real_fetch = adapter._fetch_blobs

    def recording_fetch(object_ids):
        fetched.extend(object_ids)
        return real_fetch(object_ids)

    monkeypatch.setattr(adapter, "_fetch_blobs", recording_fetch)
    adapter._module_references("agent/core.py")
    venv_blob = adapter._candidate_tree()[1][".venv/lib/big.py"][1]
    assert venv_blob not in fetched


def test_g1_blob_batch_skips_non_test_helpers_under_tests(tmp_path, monkeypatch) -> None:
    helpers = {
        "tests/conftest.py": "x = 1\n" * 5000,
        "tests/fixtures/big_fixture.py": "y = 2\n" * 5000,
        "tests/unit/helpers.py": "z = 3\n" * 5000,
        "tests/e2e/test_pruned.py": "import agent.core\n",
    }
    # Cold, test-local parse caches: what is fetched cannot depend on test order.
    for cache in ("_PENDING_BLOBS", "_REFERENCES_BY_BLOB", "_PREFIXES_BY_BLOB"):
        monkeypatch.setattr(adapter, cache, {})
    _candidate(
        tmp_path,
        monkeypatch,
        {
            "agent/__init__.py": "# g1 helper-scope package\n",
            "agent/core.py": "import os  # g1 helper-scope module\n",
            # Selected through its import, not by its file stem, so it is read.
            "tests/unit/test_g1_probe.py": "import agent.core  # g1 helper-scope probe\n",
            **helpers,
        },
    )
    fetched: list[str] = []
    real_fetch = adapter._fetch_blobs

    def recording_fetch(object_ids):
        fetched.extend(object_ids)
        return real_fetch(object_ids)

    monkeypatch.setattr(adapter, "_fetch_blobs", recording_fetch)
    assert adapter.select_tests(["agent/core.py"]) == (["tests/unit/test_g1_probe.py"], False)
    entries = adapter._candidate_tree()[1]
    assert not {entries[path][1] for path in helpers} & set(fetched)
    assert entries["tests/unit/test_g1_probe.py"][1] in fetched
    # A helper read directly is still fetched (and parsed) on demand.
    assert "os" not in adapter._module_references("tests/conftest.py")
    assert entries["tests/conftest.py"][1] in fetched


def test_blob_batch_universe_is_exactly_what_discovery_returns() -> None:
    with adapter._plan_snapshot():
        _, entries = adapter._candidate_tree()
        universe = {
            path
            for path, (mode, _) in entries.items()
            if mode in adapter._REGULAR_MODES and path.endswith(".py") and adapter._in_discoverable_universe(path)
        }
        discovered = set(adapter.discover_python_sources()) | set(adapter.discover_tests())
    assert universe == discovered
    assert "tests/ci/conftest.py" not in universe
