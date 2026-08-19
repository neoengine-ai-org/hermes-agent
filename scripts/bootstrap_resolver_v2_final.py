#!/usr/bin/env python3
"""Observable final entry point for hardened Hermes recovery.

Before the merged core runs, this entry point detects only the narrow case in
which an admitted repository-local venv still has the expected fingerprint but
fails the real closure/import validator. It removes that generated venv once;
the hardened core then recreates it from `uv.lock`, validates, and truthfully
emits `environment_recovered=true` and the RECOVERED state.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
HARDENED = ROOT / "scripts/bootstrap_resolver_v2_hardened.py"


def _cli_value(flag: str, default: str | None) -> str | None:
    try:
        index = sys.argv.index(flag)
    except ValueError:
        return default
    if index + 1 >= len(sys.argv):
        return default
    return sys.argv[index + 1]


def load_hardened() -> Any:
    spec = importlib.util.spec_from_file_location(
        "hermes_bootstrap_resolver_v2_hardened_entry",
        HARDENED,
    )
    if spec is None or spec.loader is None:
        raise SystemExit("HERMES_SELF_HEALING_BOOTSTRAP_BLOCKED: hardening unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def prepare_observable_rebuild(core: Any) -> None:
    if len(sys.argv) <= 1 or sys.argv[1] != "ensure" or "--repair" not in sys.argv:
        return
    root_value = _cli_value("--root", None)
    root = core.root_from_git(root_value)
    policy_rel = str(
        _cli_value(
            "--policy",
            "config/hermes-bootstrap-acquisition-v2.json",
        )
    )
    pin_rel = str(
        _cli_value(
            "--pin",
            "config/hermes-bootstrap-acquisition-v2.sha256",
        )
    )
    venv_rel = str(_cli_value("--venv", ".venv"))
    core.load_policy(root, policy_rel, pin_rel)
    venv = core.contained(root, venv_rel)
    python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    fingerprint = venv / ".hermes-self-healing-bootstrap-v2.json"
    if not (python.is_file() and fingerprint.is_file()):
        return

    result = subprocess.run(
        [
            str(python),
            "scripts/validate_hermes_bootstrap_closure.py",
            "--root",
            ".",
            "--receipt-venv",
            str(venv),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if result.returncode == 0:
        return
    if venv.is_symlink():
        raise core.RecoveryError("PATH_ESCAPE", venv_rel)
    shutil.rmtree(venv)


def main() -> int:
    hardened = load_hardened()
    core = hardened.load_core()
    prepare_observable_rebuild(core)
    return int(core.main())


if __name__ == "__main__":
    raise SystemExit(main())
