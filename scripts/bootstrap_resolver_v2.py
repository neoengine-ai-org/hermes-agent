#!/usr/bin/env python3
"""Hermes product-owned self-healing bootstrap resolver.

Restores exact repository artifacts from admitted Git objects, creates an exact
lock-backed repository-local environment with uv, validates it, and retries one
declared operation. Shared-home environments are never receipt authority.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

READY = "HERMES_SELF_HEALING_BOOTSTRAP_READY"
RECOVERED = "HERMES_SELF_HEALING_BOOTSTRAP_RECOVERED"
BLOCKED = "HERMES_SELF_HEALING_BOOTSTRAP_BLOCKED"

class RecoveryError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def root_from_git(value: str | None) -> Path:
    cwd = Path(value).resolve() if value else Path.cwd()
    result = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                            text=True, capture_output=True, check=False)
    if result.returncode:
        raise RecoveryError("ROOT_NOT_GIT_WORKTREE", result.stderr.strip())
    return Path(result.stdout.strip()).resolve()

def relative(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise RecoveryError("PATH_ESCAPE", value)
    return path

def contained(root: Path, value: str) -> Path:
    rel = relative(value)
    probe = root
    for part in rel.parts:
        probe = probe / part
        if probe.is_symlink():
            raise RecoveryError("PATH_ESCAPE", rel.as_posix())
    target = root / rel
    try:
        target.parent.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise RecoveryError("PATH_ESCAPE", rel.as_posix()) from exc
    if target.exists() and target.is_symlink():
        raise RecoveryError("PATH_ESCAPE", rel.as_posix())
    return target

@contextlib.contextmanager
def lock(path: Path, timeout: int):
    deadline = time.monotonic() + timeout
    while True:
        try:
            path.mkdir(parents=True)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RecoveryError("LOCK_TIMEOUT", str(path))
            time.sleep(0.1)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            path.rmdir()

def atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=".bootstrap-repair-", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        with contextlib.suppress(OSError):
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()

def git_blob(root: Path, ref: str, path: str) -> tuple[bytes, str]:
    shown = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{path}"],
                           capture_output=True, check=False)
    if shown.returncode:
        raise RecoveryError("SOURCE_UNAVAILABLE", f"{ref}:{path}")
    identity = subprocess.run(["git", "-C", str(root), "rev-parse", f"{ref}:{path}"],
                              text=True, capture_output=True, check=False)
    if identity.returncode:
        raise RecoveryError("SOURCE_IDENTITY_UNAVAILABLE", f"{ref}:{path}")
    return shown.stdout, identity.stdout.strip()

def path_dirty(root: Path, rel: Path) -> bool:
    result = subprocess.run(["git", "-C", str(root), "status", "--porcelain=v1", "--", rel.as_posix()],
                            text=True, capture_output=True, check=False)
    return bool(result.stdout.strip())

def load_policy(root: Path, policy_rel: str, pin_rel: str) -> tuple[dict[str, Any], str]:
    policy = contained(root, policy_rel)
    pin = contained(root, pin_rel)
    raw = policy.read_bytes()
    expected = pin.read_text(encoding="utf-8").split()[0]
    observed = sha256(raw)
    if observed != expected:
        raise RecoveryError("POLICY_PIN_MISMATCH", policy_rel)
    value = json.loads(raw)
    if value.get("schema_version") != "hermes.self-healing-bootstrap-acquisition/2.0.0":
        raise RecoveryError("POLICY_INVALID", policy_rel)
    if value.get("shared_home_authority_allowed") is not False:
        raise RecoveryError("POLICY_AUTHORITY_INVALID", "shared-home authority must remain false")
    return value, observed

def repair_artifacts(root: Path, policy: dict[str, Any], repair: bool) -> list[str]:
    changed: list[str] = []
    lock_root = root / ".bootstrap" / "locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    for item in policy["artifacts"]:
        with lock(lock_root / f"{item['dependency_id']}.lock", int(item["lock_timeout_seconds"])):
            data, blob = git_blob(root, item["source_ref"], item["source_path"])
            if blob != item["source_blob_sha"]:
                raise RecoveryError("SOURCE_BLOB_MISMATCH", item["dependency_id"])
            target_rel = relative(item["destination"])
            target = contained(root, item["destination"])
            if target.is_file():
                if target.stat().st_nlink != 1:
                    raise RecoveryError("HARDLINK_ALIAS", target_rel.as_posix())
                current = subprocess.run(["git", "-C", str(root), "hash-object", "--", target_rel.as_posix()],
                                         text=True, capture_output=True, check=False)
                if current.returncode == 0 and current.stdout.strip() == blob:
                    continue
                if path_dirty(root, target_rel):
                    raise RecoveryError("USER_MODIFIED_CONFLICT", target_rel.as_posix())
            if not repair:
                raise RecoveryError("REPAIR_REQUIRED", target_rel.as_posix())
            atomic_write(target, data, int(item["mode"], 8))
            current = subprocess.run(["git", "-C", str(root), "hash-object", "--", target_rel.as_posix()],
                                     text=True, capture_output=True, check=False)
            if current.returncode or current.stdout.strip() != blob:
                raise RecoveryError("POST_WRITE_BLOB_MISMATCH", target_rel.as_posix())
            changed.append(target_rel.as_posix())
    return changed

def ensure_environment(root: Path, policy: dict[str, Any], venv_rel: str, repair: bool, policy_digest: str) -> tuple[Path, bool]:
    target = contained(root, venv_rel)
    if target.name not in {".venv", ".bootstrap-proof-venv"}:
        raise RecoveryError("VENV_PATH_NOT_ADMITTED", venv_rel)
    fingerprint = target / ".hermes-self-healing-bootstrap-v2.json"
    expected = {
        "schema_version": "hermes.self-healing-bootstrap-environment/2.0.0",
        "policy_sha256": policy_digest,
        "source_commit": policy["source_commit"],
        "lock_blob_sha": policy["lock_blob_sha"],
        "python": policy["python"],
    }
    python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if python.is_file() and fingerprint.is_file():
        try:
            if json.loads(fingerprint.read_text(encoding="utf-8")) == expected:
                return python, False
        except (OSError, json.JSONDecodeError):
            pass
    if not repair:
        raise RecoveryError("ENVIRONMENT_REPAIR_REQUIRED", venv_rel)
    uv = shutil.which("uv")
    if uv is None:
        raise RecoveryError("HUMAN_AUTH_REQUIRED_TOOL_UV", "uv is not installed in the admitted runtime")
    if target.exists():
        resolved = target.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise RecoveryError("PATH_ESCAPE", venv_rel) from exc
        shutil.rmtree(target)
    run_env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(target), "PYTHONNOUSERSITE": "1"}
    for argv in (
        [uv, "venv", str(target), "--python", policy["python"]],
        [uv, "sync", "--locked", "--python", str(python)],
    ):
        result = subprocess.run(argv, cwd=root, env=run_env, text=True,
                                capture_output=True, timeout=int(policy["environment_timeout_seconds"]),
                                check=False)
        if result.returncode:
            raise RecoveryError("LOCKED_ENVIRONMENT_REPAIR_FAILED", result.stderr[-4000:] or result.stdout[-4000:])
    if not python.is_file():
        raise RecoveryError("ENVIRONMENT_PYTHON_MISSING", str(python))
    atomic_write(fingerprint, (json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n").encode(), 0o600)
    return python, True

def validate(root: Path, python: Path, venv: Path, timeout: int) -> str:
    result = subprocess.run(
        [str(python), "scripts/validate_hermes_bootstrap_closure.py",
         "--root", ".", "--receipt-venv", str(venv)],
        cwd=root, text=True, capture_output=True, timeout=timeout, check=False,
    )
    if result.returncode:
        raise RecoveryError("POST_REPAIR_VALIDATION_FAILED", result.stderr[-4000:] or result.stdout[-4000:])
    return sha256(result.stdout.encode())

def retry_operation(root: Path, python: Path, policy: dict[str, Any], operation_id: str | None) -> dict[str, Any] | None:
    if operation_id is None:
        return None
    operation = policy["operations"].get(operation_id)
    if not operation or operation.get("retry_limit") != 1:
        raise RecoveryError("OPERATION_NOT_ADMITTED", operation_id)
    argv = [str(python) if token == "{python}" else token for token in operation["argv"]]
    result = subprocess.run(argv, cwd=root, text=True, capture_output=True,
                            timeout=int(operation["timeout_seconds"]), check=False)
    if result.returncode:
        raise RecoveryError("OPERATION_RETRY_FAILED", result.stderr[-4000:] or result.stdout[-4000:])
    return {"operation_id": operation_id, "attempts": 1, "result": "PASS",
            "stdout_sha256": sha256(result.stdout.encode())}

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("ensure", "diagnose"))
    parser.add_argument("--root")
    parser.add_argument("--policy", default="config/hermes-bootstrap-acquisition-v2.json")
    parser.add_argument("--pin", default="config/hermes-bootstrap-acquisition-v2.sha256")
    parser.add_argument("--venv", default=".venv")
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--operation-id")
    args = parser.parse_args()
    try:
        root = root_from_git(args.root)
        policy, policy_digest = load_policy(root, args.policy, args.pin)
        changed = repair_artifacts(root, policy, args.repair and args.command == "ensure")
        python, environment_changed = ensure_environment(
            root, policy, args.venv, args.repair and args.command == "ensure", policy_digest
        )
        validation_digest = validate(root, python, python.parent.parent, int(policy["validation_timeout_seconds"]))
        retry = retry_operation(root, python, policy, args.operation_id)
        state = RECOVERED if changed or environment_changed else READY
        print(json.dumps({
            "state": state,
            "changed": sorted(changed),
            "environment_recovered": environment_changed,
            "environment": str(python.parent.parent.relative_to(root)),
            "validation_sha256": validation_digest,
            "operation_retry": retry,
            "shared_home_authority": "REFUSED",
        }, sort_keys=True))
        return 0
    except (RecoveryError, OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        code = exc.code if isinstance(exc, RecoveryError) else type(exc).__name__
        print(json.dumps({"state": BLOCKED, "code": code, "message": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
