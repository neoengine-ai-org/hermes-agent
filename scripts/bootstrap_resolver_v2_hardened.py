#!/usr/bin/env python3
"""Final hardening launcher for Hermes self-healing bootstrap V2.

The merged resolver remains byte-preserved. This launcher supplies crash-safe
kernel locking, a fixed product-owned operation registry, and exactly one
lock-backed environment rebuild when a fingerprinted environment fails the
post-repair import/closure proof.
"""
from __future__ import annotations

import contextlib
import errno
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Type

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "scripts/bootstrap_resolver_v2.py"


class CrashSafeFileLock:
    def __init__(
        self,
        path: Path,
        timeout_seconds: float,
        *,
        error_type: Type[RuntimeError] = RuntimeError,
    ) -> None:
        self.path = path
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self.error_type = error_type
        self.fd: int | None = None
        self.backend: str | None = None

    def _error(self, code: str, message: str) -> RuntimeError:
        try:
            return self.error_type(code, message)  # type: ignore[misc]
        except TypeError:
            return self.error_type(f"{code}: {message}")

    def _open(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        absolute = self.path.absolute()
        probe = Path(absolute.anchor)
        for part in absolute.parts[1:-1]:
            probe = probe / part
            if probe.is_symlink():
                raise self._error(
                    "RECOVERY_LOCK_PATH_UNSAFE",
                    f"symlinked lock parent refused: {probe}",
                )
        try:
            existing = os.lstat(self.path)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode):
                raise self._error(
                    "RECOVERY_LOCK_PATH_UNSAFE",
                    f"symlink lock path refused: {self.path}",
                )
            if not stat.S_ISREG(existing.st_mode):
                raise self._error(
                    "RECOVERY_LOCK_PATH_UNSAFE",
                    f"lock path must be a regular file: {self.path}",
                )
            if existing.st_nlink != 1:
                raise self._error(
                    "RECOVERY_LOCK_PATH_UNSAFE",
                    f"hardlinked lock file refused: {self.path}",
                )
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise self._error(
                "RECOVERY_LOCK_PATH_UNSAFE",
                f"cannot safely open lock file {self.path}: {exc}",
            ) from exc
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise self._error(
                    "RECOVERY_LOCK_PATH_UNSAFE",
                    f"opened lock is not a unique regular file: {self.path}",
                )
            observed = os.lstat(self.path)
            if (
                getattr(opened, "st_ino", 0)
                and getattr(observed, "st_ino", 0)
                and (
                    opened.st_dev != observed.st_dev
                    or opened.st_ino != observed.st_ino
                )
            ):
                raise self._error(
                    "RECOVERY_LOCK_PATH_UNSAFE",
                    f"lock path changed during open: {self.path}",
                )
            with contextlib.suppress(OSError, AttributeError):
                os.fchmod(fd, 0o600)
            return fd
        except Exception:
            os.close(fd)
            raise

    def _try_lock(self, fd: int) -> bool:
        if os.name == "nt":
            import msvcrt

            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                self.backend = "msvcrt.locking"
                return True
            except OSError as exc:
                if exc.errno in {
                    errno.EACCES,
                    errno.EAGAIN,
                    errno.EDEADLK,
                    getattr(errno, "EDEADLOCK", errno.EDEADLK),
                }:
                    return False
                raise
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.backend = "fcntl.flock"
            return True
        except BlockingIOError:
            return False
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise

    def __enter__(self) -> "CrashSafeFileLock":
        fd = self._open()
        deadline = time.monotonic() + self.timeout_seconds
        try:
            while not self._try_lock(fd):
                if time.monotonic() >= deadline:
                    raise self._error(
                        "LOCK_TIMEOUT",
                        f"timed out waiting for recovery lock {self.path}",
                    )
                time.sleep(0.05)
            self.fd = fd
            payload = json.dumps(
                {
                    "schema_version": "bootstrap-recovery-lock/2.0.0",
                    "pid": os.getpid(),
                    "acquired_epoch": int(time.time()),
                    "backend": self.backend,
                    "authority": "KERNEL_ADVISORY_LOCK",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload)
            os.fsync(fd)
            return self
        except Exception:
            os.close(fd)
            raise

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.fd is None:
            return
        fd = self.fd
        self.fd = None
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                with contextlib.suppress(OSError):
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _cli_value(flag: str, default: str) -> str:
    try:
        index = sys.argv.index(flag)
    except ValueError:
        return default
    if index + 1 >= len(sys.argv):
        return default
    return sys.argv[index + 1]


def load_core() -> Any:
    spec = importlib.util.spec_from_file_location(
        "hermes_bootstrap_resolver_v2_core",
        CORE,
    )
    if spec is None or spec.loader is None:
        raise SystemExit("HERMES_SELF_HEALING_BOOTSTRAP_BLOCKED: resolver unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    original_ensure_environment = module.ensure_environment
    original_validate = module.validate
    state: dict[str, Any] = {
        "environment_changed": None,
        "rebuild_attempted": False,
    }

    @contextlib.contextmanager
    def crash_safe_lock(path: Path, timeout_seconds: int):
        lock_path = path.with_name(path.name + ".v2")
        with CrashSafeFileLock(
            lock_path,
            timeout_seconds,
            error_type=module.RecoveryError,
        ):
            yield

    def tracked_ensure_environment(
        root: Path,
        policy: dict[str, Any],
        venv_rel: str,
        repair: bool,
        policy_digest: str,
    ) -> tuple[Path, bool]:
        result = original_ensure_environment(
            root,
            policy,
            venv_rel,
            repair,
            policy_digest,
        )
        state["environment_changed"] = result[1]
        return result

    def hardened_validate(
        root: Path,
        python: Path,
        venv: Path,
        timeout: int,
    ) -> str:
        try:
            return original_validate(root, python, venv, timeout)
        except module.RecoveryError as error:
            can_rebuild = (
                error.code == "POST_REPAIR_VALIDATION_FAILED"
                and len(sys.argv) > 1
                and sys.argv[1] == "ensure"
                and "--repair" in sys.argv
                and state["environment_changed"] is False
                and state["rebuild_attempted"] is False
            )
            if not can_rebuild:
                raise
            state["rebuild_attempted"] = True
            policy_rel = _cli_value(
                "--policy",
                "config/hermes-bootstrap-acquisition-v2.json",
            )
            pin_rel = _cli_value(
                "--pin",
                "config/hermes-bootstrap-acquisition-v2.sha256",
            )
            venv_rel = _cli_value("--venv", ".venv")
            policy, policy_digest = module.load_policy(
                root,
                policy_rel,
                pin_rel,
            )
            if venv.is_symlink():
                raise module.RecoveryError(
                    "PATH_ESCAPE",
                    venv_rel,
                )
            if venv.exists():
                shutil.rmtree(venv)
            rebuilt_python, changed = original_ensure_environment(
                root,
                policy,
                venv_rel,
                True,
                policy_digest,
            )
            if not changed:
                raise module.RecoveryError(
                    "ENVIRONMENT_REBUILD_NOT_OBSERVED",
                    venv_rel,
                )
            state["environment_changed"] = True
            return original_validate(
                root,
                rebuilt_python,
                rebuilt_python.parent.parent,
                timeout,
            )

    def fixed_retry_operation(
        root: Path,
        python: Path,
        policy: dict[str, Any],
        operation_id: str | None,
    ) -> dict[str, Any] | None:
        if operation_id is None:
            return None
        operation = policy.get("operations", {}).get(operation_id)
        if (
            operation_id != "validate-bootstrap"
            or not isinstance(operation, dict)
            or set(operation) != {"retry_limit", "timeout_seconds"}
            or operation.get("retry_limit") != 1
        ):
            raise module.RecoveryError(
                "OPERATION_NOT_ADMITTED",
                str(operation_id),
            )
        timeout = int(operation["timeout_seconds"])
        if timeout < 1 or timeout > 300:
            raise module.RecoveryError(
                "OPERATION_TIMEOUT_INVALID",
                str(timeout),
            )
        venv = python.parent.parent
        argv = [
            str(python),
            "scripts/validate_hermes_bootstrap_closure.py",
            "--root",
            ".",
            "--receipt-venv",
            str(venv),
        ]
        result = subprocess.run(
            argv,
            cwd=root,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            raise module.RecoveryError(
                "OPERATION_RETRY_FAILED",
                result.stderr[-4000:] or result.stdout[-4000:],
            )
        return {
            "operation_id": operation_id,
            "attempts": 1,
            "result": "PASS",
            "stdout_sha256": module.sha256(
                result.stdout.encode("utf-8")
            ),
        }

    module.lock = crash_safe_lock
    module.ensure_environment = tracked_ensure_environment
    module.validate = hardened_validate
    module.retry_operation = fixed_retry_operation
    return module


def main() -> int:
    return int(load_core().main())


if __name__ == "__main__":
    raise SystemExit(main())
