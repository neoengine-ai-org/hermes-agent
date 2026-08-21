from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
FINAL = ROOT / "scripts/bootstrap_resolver_v2_final.py"
POLICY = ROOT / "config/hermes-bootstrap-acquisition-v2.json"
PIN = ROOT / "config/hermes-bootstrap-acquisition-v2.sha256"


def run(
    argv: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 900,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def load_final():
    spec = importlib.util.spec_from_file_location(
        "hermes_final_hardening_test_subject",
        FINAL,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def crash_lock_class():
    return load_final().load_hardened().CrashSafeFileLock


def test_policy_pin_fixed_operation_and_final_entrypoint() -> None:
    raw = POLICY.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PIN.read_text().split()[0]
    policy = json.loads(raw)
    assert policy["stage1"] == {
        "blob_sha": "5cda1fb8f8cd60189c2a87cd117b831d9e6be8bd",
        "path": "scripts/bootstrap_resolver_v2_final.py",
    }
    assert policy["operations"]["validate-bootstrap"] == {
        "retry_limit": 1,
        "timeout_seconds": 180,
    }
    assert "argv" not in policy["operations"]["validate-bootstrap"]


def test_malformed_lock_payload_recovers_and_persists(tmp_path: Path) -> None:
    lock_class = crash_lock_class()
    lock_path = tmp_path / "resolver.lock.v2"
    lock_path.write_text("{truncated", encoding="utf-8")
    with lock_class(lock_path, 1):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["authority"] == "KERNEL_ADVISORY_LOCK"
    assert lock_path.is_file()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_abrupt_exit_releases_kernel_lock(tmp_path: Path) -> None:
    lock_class = crash_lock_class()
    lock_path = tmp_path / "resolver.lock.v2"
    child = os.fork()
    if child == 0:
        lock = lock_class(lock_path, 1)
        lock.__enter__()
        os._exit(0)
    _, status = os.waitpid(child, 0)
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 0
    assert lock_path.is_file()
    with lock_class(lock_path, 1):
        pass


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_live_owner_is_preserved(tmp_path: Path) -> None:
    lock_class = crash_lock_class()
    lock_path = tmp_path / "resolver.lock.v2"
    with lock_class(lock_path, 1) as held:
        child = os.fork()
        if child == 0:
            if held.fd is not None:
                os.close(held.fd)
            try:
                with lock_class(lock_path, 0.15):
                    os._exit(4)
            except Exception as exc:  # noqa: BLE001 - child process proof
                os._exit(0 if "LOCK_TIMEOUT" in repr(exc.args) else 3)
        _, status = os.waitpid(child, 0)
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 0


@pytest.mark.timeout(300)
def test_fingerprinted_corrupt_environment_rebuilds_once(tmp_path: Path) -> None:
    checkout = tmp_path / "hermes"
    clone = run(
        ["git", "clone", "--no-local", "--quiet", str(ROOT), str(checkout)],
        tmp_path,
    )
    assert clone.returncode == 0, clone.stderr
    remote = run(
        [
            "git",
            "remote",
            "set-url",
            "origin",
            "https://github.com/neoengine-ai-org/hermes-agent.git",
        ],
        checkout,
    )
    assert remote.returncode == 0, remote.stderr
    env = {
        **os.environ,
        "PYTHONNOUSERSITE": "1",
        "UV_NO_PROGRESS": "1",
    }

    first = run(
        [
            sys.executable,
            "-S",
            "scripts/bootstrap_stage0_v2.py",
            "ensure",
            "--repair",
            "--operation-id",
            "validate-bootstrap",
        ],
        checkout,
        env,
    )
    assert first.returncode == 0, first.stderr
    first_receipt = json.loads(first.stdout)
    assert first_receipt["operation_retry"]["attempts"] == 1

    python = checkout / ".venv/bin/python"
    locate = run(
        [
            str(python),
            "-c",
            "import pathlib,yaml; print(pathlib.Path(yaml.__file__).parent)",
        ],
        checkout,
        env,
    )
    assert locate.returncode == 0, locate.stderr
    yaml_directory = Path(locate.stdout.strip())
    shutil.rmtree(yaml_directory)
    fingerprint = checkout / ".venv/.hermes-self-healing-bootstrap-v2.json"
    assert fingerprint.is_file()

    repaired = run(
        [
            sys.executable,
            "-S",
            "scripts/bootstrap_stage0_v2.py",
            "ensure",
            "--repair",
            "--operation-id",
            "validate-bootstrap",
        ],
        checkout,
        env,
    )
    assert repaired.returncode == 0, repaired.stderr
    receipt = json.loads(repaired.stdout)
    assert receipt["state"] == "HERMES_SELF_HEALING_BOOTSTRAP_RECOVERED"
    assert receipt["environment_recovered"] is True
    assert receipt["operation_retry"]["attempts"] == 1

    proof = run(
        [str(python), "-c", "import yaml; print('ok')"],
        checkout,
        env,
    )
    assert proof.returncode == 0, proof.stderr

    no_op = run(
        [
            sys.executable,
            "-S",
            "scripts/bootstrap_stage0_v2.py",
            "ensure",
            "--repair",
        ],
        checkout,
        env,
    )
    assert no_op.returncode == 0, no_op.stderr
    assert json.loads(no_op.stdout)["state"] == "HERMES_SELF_HEALING_BOOTSTRAP_READY"
