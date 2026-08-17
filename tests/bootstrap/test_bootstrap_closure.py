"""Hostile tests for checkout-local Hermes bootstrap closure."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_PATH = ROOT / "scripts" / "validate_hermes_bootstrap_closure.py"
SPEC = importlib.util.spec_from_file_location("hermes_bootstrap_validator", VALIDATOR_PATH)
assert SPEC and SPEC.loader
closure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(closure)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "hermes-agent"
    root.mkdir()
    for _, relative in closure.EXPECTED_ARTIFACTS:
        source = ROOT / relative
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "bootstrap-tests@example.invalid")
    _git(root, "config", "user.name", "Bootstrap Tests")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "fixture")
    return root


def _fixture_report(
    root: Path,
    *,
    receipt_mode: bool = False,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> dict[str, object]:
    interpreter = root / ".venv" / "bin" / "python" if receipt_mode else Path(sys.executable)
    if receipt_mode:
        (root / ".git" / "info" / "exclude").write_text(".venv/\n", encoding="utf-8")
        (root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (root / ".venv" / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    if monkeypatch is not None:
        monkeypatch.setenv("PYTEST_PLUGINS", "other_plugin, pytest_live_guard")
    return closure.validate(
        root,
        receipt_mode=receipt_mode,
        interpreter=interpreter,
        run_lock_check=False,
        run_import_smoke=False,
    )


def test_clean_checkout_without_user_home_passes_and_defers_optional_providers(
) -> None:
    with pytest.MonkeyPatch.context() as isolated:
        isolated.setenv("HOME", str(ROOT / ".bootstrap-test-home-does-not-exist"))
        for key in list(os.environ):
            if key.endswith(closure.SECRET_ENV_SUFFIXES):
                isolated.delenv(key, raising=False)
        report = closure.validate(ROOT, receipt_mode=False)
    assert report["state"] == closure.READY, report["findings"]
    assert report["optional_provider_state"] == closure.OPTIONAL_UNAVAILABLE
    assert sorted(report["smoke"]["entrypoints"]) == sorted(closure.EXPECTED_ENTRYPOINTS)
    assert report["smoke"]["network"] == "disabled"
    assert report["smoke"]["home"] == "isolated"
    assert set(report["smoke"]["optional_imports_blocked"]) >= {"aiohttp", "discord"}


@pytest.mark.parametrize("relative", ["uv.lock", "run_agent.py"])
def test_missing_lock_or_entrypoint_fails(checkout: Path, relative: str) -> None:
    (checkout / relative).unlink()
    report = _fixture_report(checkout)
    assert report["state"] == closure.BLOCKED
    assert "MISSING_ARTIFACT" in {item["code"] for item in report["findings"]}


def test_changed_console_entrypoint_target_fails(checkout: Path) -> None:
    metadata = checkout / "pyproject.toml"
    metadata.write_text(
        metadata.read_text(encoding="utf-8").replace(
            'hermes-progress = "scripts.hermes_progress:main"',
            'hermes-progress = "scripts.hermes_progress:wrong"',
        ),
        encoding="utf-8",
    )
    report = _fixture_report(checkout)
    assert report["state"] == closure.BLOCKED
    assert "ENTRYPOINTS" in {item["code"] for item in report["findings"]}


def test_symlink_escape_fails(checkout: Path, tmp_path: Path) -> None:
    entrypoint = checkout / "run_agent.py"
    entrypoint.unlink()
    entrypoint.symlink_to(tmp_path / "outside.py")
    report = _fixture_report(checkout)
    assert report["state"] == closure.BLOCKED
    assert "PATH_ESCAPE" in {item["code"] for item in report["findings"]}


def test_shared_home_venv_cannot_produce_receipt_grade_pass(checkout: Path, tmp_path: Path) -> None:
    home_python = tmp_path / "home" / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    report = closure.validate(
        checkout,
        receipt_mode=True,
        interpreter=home_python,
        run_lock_check=False,
        run_import_smoke=False,
    )
    assert report["state"] == closure.SHARED_VENV
    with pytest.raises(ValueError, match="receipt-mode ready"):
        closure.build_receipt(checkout, report)


def test_repo_local_venv_symlink_to_shared_home_cannot_produce_receipt(
    checkout: Path, tmp_path: Path
) -> None:
    shared = tmp_path / "home" / ".hermes" / "hermes-agent" / "venv"
    (shared / "bin").mkdir(parents=True)
    (shared / "pyvenv.cfg").write_text("home = shared\n", encoding="utf-8")
    (checkout / ".venv").symlink_to(shared, target_is_directory=True)
    report = closure.validate(
        checkout,
        receipt_mode=True,
        interpreter=checkout / ".venv" / "bin" / "python",
        run_lock_check=False,
        run_import_smoke=False,
    )
    assert report["state"] == closure.SHARED_VENV


def test_checkout_path_alias_does_not_reject_the_same_local_venv(
    checkout: Path, tmp_path: Path
) -> None:
    (checkout / ".venv" / "bin").mkdir(parents=True)
    (checkout / ".venv" / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    alias = tmp_path / "checkout-alias"
    alias.symlink_to(checkout, target_is_directory=True)
    provenance, finding = closure.interpreter_provenance(
        alias,
        alias / ".venv" / "bin" / "python",
        receipt_venv=alias / ".venv",
    )
    assert finding is None
    assert provenance["classification"] == "ORG_NATIVE_BOOTSTRAP"
    assert provenance["venv"] == ".venv"


def test_home_pytest_plugin_cannot_affect_receipt_grade_result(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _fixture_report(checkout, receipt_mode=True, monkeypatch=monkeypatch)
    assert report["state"] == closure.SHARED_VENV
    assert "HOME_PLUGIN" in {item["code"] for item in report["findings"]}


def test_sibling_checkout_cannot_satisfy_missing_module(checkout: Path, tmp_path: Path) -> None:
    (checkout / "run_agent.py").unlink()
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    shutil.copy2(ROOT / "run_agent.py", sibling / "run_agent.py")
    report = _fixture_report(checkout)
    assert report["state"] == closure.BLOCKED


def test_global_hermes_executable_cannot_satisfy_missing_local_entrypoint(
    checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (checkout / "hermes_cli" / "main.py").unlink()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "hermes"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    report = _fixture_report(checkout)
    assert report["state"] == closure.BLOCKED


@pytest.mark.parametrize(
    "relative",
    [
        "config/hermes-bootstrap-closure-v1.json",
        "scripts/validate_hermes_bootstrap_closure.py",
        "uv.lock",
        "pyproject.toml",
        "AGENTS.md",
    ],
)
def test_receipt_changes_with_protected_inputs(checkout: Path, relative: str) -> None:
    report = _fixture_report(checkout, receipt_mode=True)
    assert report["state"] == closure.READY
    first = closure.build_receipt(checkout, report)
    target = checkout / relative
    target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    _git(checkout, "add", "--", relative)
    _git(checkout, "commit", "-qm", "changed protected input")
    changed = _fixture_report(checkout, receipt_mode=True)
    assert changed["state"] == closure.READY
    second = closure.build_receipt(checkout, changed)
    assert second["receipt_id"] != first["receipt_id"]


def test_dirty_checkout_cannot_emit_head_bound_receipt(checkout: Path) -> None:
    report = _fixture_report(checkout, receipt_mode=True)
    assert report["state"] == closure.READY
    target = checkout / "AGENTS.md"
    target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    dirty = _fixture_report(checkout, receipt_mode=True)
    assert dirty["state"] == closure.BLOCKED
    assert "DIRTY_CHECKOUT" in {item["code"] for item in dirty["findings"]}
    with pytest.raises(ValueError, match="receipt-mode ready"):
        closure.build_receipt(checkout, dirty)


def test_cached_ready_report_cannot_emit_after_worktree_mutation(checkout: Path) -> None:
    report = _fixture_report(checkout, receipt_mode=True)
    assert report["state"] == closure.READY
    target = checkout / "AGENTS.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nmutated\n", encoding="utf-8")
    with pytest.raises(ValueError, match="clean checkout"):
        closure.build_receipt(checkout, report)


def test_hardlinked_required_artifact_fails_closed(checkout: Path, tmp_path: Path) -> None:
    artifact = checkout / "AGENTS.md"
    outside = tmp_path / "outside-agents.md"
    outside.write_bytes(artifact.read_bytes())
    artifact.unlink()
    os.link(outside, artifact)
    report = _fixture_report(checkout)
    assert report["state"] == closure.BLOCKED
    assert "MISSING_ARTIFACT" in {item["code"] for item in report["findings"]}


@pytest.mark.parametrize("alias", [r"config\hermes-bootstrap-closure-v1.json", "docs/e\u0301vidence.md"])
def test_noncanonical_artifact_alias_is_rejected(checkout: Path, alias: str) -> None:
    _path, finding = closure._contained_regular_file(checkout, alias)
    assert finding is not None
    assert finding["code"] == "PATH_ESCAPE"


def test_receipt_output_cannot_overwrite_product_source(checkout: Path) -> None:
    report = _fixture_report(checkout, receipt_mode=True)
    assert report["state"] == closure.READY
    original = (checkout / "AGENTS.md").read_bytes()
    with pytest.raises(ValueError, match="output path"):
        closure._write_receipt(checkout, "AGENTS.md", closure.build_receipt(checkout, report))
    assert (checkout / "AGENTS.md").read_bytes() == original


def test_receipt_changes_with_head_even_when_tree_is_unchanged(checkout: Path) -> None:
    report = _fixture_report(checkout, receipt_mode=True)
    first = closure.build_receipt(checkout, report)
    _git(checkout, "commit", "--allow-empty", "-qm", "new source identity")
    second = closure.build_receipt(checkout, _fixture_report(checkout, receipt_mode=True))
    assert second["source"]["tree"] == first["source"]["tree"]
    assert second["source"]["head"] != first["source"]["head"]
    assert second["receipt_id"] != first["receipt_id"]


def _fake_venv(path: Path) -> None:
    bin_dir = path / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "activate").write_text("# test fixture\n", encoding="utf-8")
    wrapper = bin_dir / "python"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)


def _runner_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "runner-repo"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "run_tests.sh", scripts / "run_tests.sh")
    (scripts / "validate_hermes_bootstrap_closure.py").write_text(
        'print("VALIDATOR_RAN")\n', encoding="utf-8"
    )
    (scripts / "run_tests_parallel.py").write_text(
        "import json, os\n"
        "print('RUNNER_ENV=' + json.dumps({"
        "'HOME': os.environ.get('HOME'), "
        "'HERMES_HOME': os.environ.get('HERMES_HOME'), "
        "'PYTEST_PLUGINS': os.environ.get('PYTEST_PLUGINS')}))\n",
        encoding="utf-8",
    )
    return root


def test_runner_preserves_explicit_shared_home_developer_convenience(tmp_path: Path) -> None:
    root = _runner_fixture(tmp_path)
    home = tmp_path / "developer-home"
    _fake_venv(home / ".hermes" / "hermes-agent" / "venv")
    plugin = home / ".hermes" / "pytest_live_guard.py"
    plugin.parent.mkdir(parents=True, exist_ok=True)
    plugin.write_text("# test fixture\n", encoding="utf-8")
    result = subprocess.run(
        [str(root / "scripts" / "run_tests.sh"), "--allow-shared-venv"],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "interpreter_provenance=shared-home-non-receipt" in result.stdout
    assert '"PYTEST_PLUGINS": "pytest_live_guard"' in result.stdout


def test_runner_receipt_mode_uses_isolated_home_and_no_home_plugin(tmp_path: Path) -> None:
    root = _runner_fixture(tmp_path)
    _fake_venv(root / ".venv")
    home = tmp_path / "developer-home"
    plugin = home / ".hermes" / "pytest_live_guard.py"
    plugin.parent.mkdir(parents=True, exist_ok=True)
    plugin.write_text("# test fixture\n", encoding="utf-8")
    result = subprocess.run(
        [str(root / "scripts" / "run_tests.sh"), "--receipt-mode"],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "interpreter_provenance=repo-local" in result.stdout
    assert "VALIDATOR_RAN" in result.stdout
    env_line = next(line for line in result.stdout.splitlines() if line.startswith("RUNNER_ENV="))
    observed = json.loads(env_line.split("=", 1)[1])
    assert observed["HOME"] != str(home)
    assert observed["HERMES_HOME"] == f"{observed['HOME']}/.hermes"
    assert observed["PYTEST_PLUGINS"] is None
