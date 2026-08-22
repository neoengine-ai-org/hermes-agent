#!/usr/bin/env python3
"""Validate Hermes checkout-local runtime bootstrap and emit pass-only receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import tomllib
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any


VERSION = "1.0.0"
READY = "HERMES_BOOTSTRAP_CLOSURE_READY"
BLOCKED = "HERMES_BOOTSTRAP_CLOSURE_BLOCKED"
SHARED_VENV = "HERMES_BOOTSTRAP_SHARED_VENV_NON_RECEIPT"
OPTIONAL_UNAVAILABLE = "HERMES_BOOTSTRAP_OPTIONAL_PROVIDER_UNAVAILABLE"
MANIFEST_INVALID = "HERMES_BOOTSTRAP_MANIFEST_INVALID"
MANIFEST_PATH = Path("config/hermes-bootstrap-closure-v1.json")
VALIDATOR_PATH = Path("scripts/validate_hermes_bootstrap_closure.py")
EXPECTED_ENTRYPOINTS = {
    "hermes": "hermes_cli.main:main",
    "hermes-agent": "run_agent:main",
    "hermes-acp": "acp_adapter.entry:main",
    "hermes-progress": "scripts.hermes_progress:main",
}
EXPECTED_ARTIFACTS = (
    ("ci-admission", ".github/workflows/tests.yml"),
    ("agent-instructions", "AGENTS.md"),
    ("readme", "README.md"),
    ("acp-entrypoint", "acp_adapter/entry.py"),
    ("bootstrap-manifest", MANIFEST_PATH.as_posix()),
    ("windows-bootstrap", "hermes_bootstrap.py"),
    ("path-constants", "hermes_constants.py"),
    ("cli-entrypoint", "hermes_cli/main.py"),
    ("package-metadata", "pyproject.toml"),
    ("agent-entrypoint", "run_agent.py"),
    ("progress-entrypoint", "scripts/hermes_progress.py"),
    ("bootstrap-stage0", "scripts/bootstrap_stage0_v2.py"),
    ("test-runner", "scripts/run_tests.sh"),
    ("test-runner-v1", "scripts/run_tests_v1.sh"),
    ("parallel-test-runner", "scripts/run_tests_parallel.py"),
    ("bootstrap-validator", VALIDATOR_PATH.as_posix()),
    ("hostile-tests", "tests/bootstrap/test_bootstrap_closure.py"),
    ("lockfile", "uv.lock"),
)
EXPECTED_PROJECT = {
    "name": "hermes-agent",
    "python_requires": ">=3.11",
    "lock_validation": "uv lock --check",
}
EXPECTED_PROVENANCE = {
    "accepted_repo_local_venvs": [".venv", ".bootstrap-proof-venv"],
    "explicit_repo_local_venv_allowed": True,
    "shared_home_venv_allowed": False,
    "home_pytest_plugin_allowed": False,
}
EXPECTED_OPTIONAL_SURFACES = [
    {
        "dependency_id": "provider-and-plugin-extras",
        "dependency_class": "CHILD_OR_PROVIDER_OPTIONAL",
        "local_required": False,
        "status": "DEFERRED",
    },
    {
        "dependency_id": "home-config-credentials-and-cache",
        "dependency_class": "ASSEMBLED_WORKSPACE_ONLY",
        "local_required": False,
        "status": "NOT_BOOTSTRAP_PROOF",
    },
]
EXPECTED_RECEIPT = {
    "schema_version": "hermes.bootstrap-closure-receipt/1.0",
    "deterministic": True,
    "pass_only": True,
}
EXPECTED_NON_CLAIMS = {
    "api_credential_or_provider_activation",
    "branch_protection_or_review_bypass",
    "external_service_call",
    "gateway_or_platform_deployment",
    "global_installation",
    "package_publication_or_release",
    "production_readiness",
    "user_home_mutation",
}
OPTIONAL_IMPORT_ROOTS = {
    "acp",
    "aiohttp",
    "aiohttp_socks",
    "aiosqlite",
    "alibabacloud_dingtalk",
    "anthropic",
    "asyncpg",
    "azure",
    "boto3",
    "daytona",
    "dingtalk_stream",
    "discord",
    "edge_tts",
    "elevenlabs",
    "exa_py",
    "fal_client",
    "fastapi",
    "firecrawl",
    "google",
    "googleapiclient",
    "hindsight_client",
    "honcho",
    "lark_oapi",
    "markdown",
    "mautrix",
    "mcp",
    "modal",
    "numpy",
    "parallel",
    "ptyprocess",
    "qrcode",
    "simple_term_menu",
    "slack_bolt",
    "slack_sdk",
    "sounddevice",
    "telegram",
    "uvicorn",
    "vercel",
    "youtube_transcript_api",
}
SECRET_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIAL")


def _finding(code: str, message: str, path: str | None = None) -> dict[str, str]:
    result = {"code": code, "message": message}
    if path is not None:
        result["path"] = path
    return result


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=check
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _head_blob(root: Path, relative_text: str) -> tuple[str, bytes]:
    blob_sha = _git(root, "rev-parse", f"HEAD:{relative_text}").stdout.strip()
    blob = subprocess.run(
        ["git", "-C", str(root), "cat-file", "blob", blob_sha],
        capture_output=True,
        check=True,
    ).stdout
    return blob_sha, blob


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _contained_regular_file(root: Path, relative_text: str) -> tuple[Path | None, dict[str, str] | None]:
    if not isinstance(relative_text, str) or not relative_text:
        return None, _finding("MANIFEST_PATH_INVALID", "artifact path must be a non-empty string")
    pure = PurePosixPath(relative_text)
    if (
        pure.is_absolute() or ".." in pure.parts or pure == PurePosixPath(".")
        or "\\" in relative_text or "\x00" in relative_text
        or relative_text != unicodedata.normalize("NFC", relative_text)
        or pure.as_posix() != relative_text
    ):
        return None, _finding("PATH_ESCAPE", "artifact path must be checkout-relative", relative_text)
    relative = Path(*pure.parts)
    probe = root
    for part in relative.parts:
        probe = probe / part
        if probe.is_symlink():
            return None, _finding("PATH_ESCAPE", "artifact path contains a symlink", relative_text)
    candidate = root / relative
    try:
        candidate.resolve(strict=False).relative_to(root)
    except (OSError, ValueError) as error:
        return None, _finding("PATH_ESCAPE", f"artifact escapes checkout: {error}", relative_text)
    try:
        metadata = os.lstat(candidate)
    except OSError:
        metadata = None
    if metadata is None or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        return None, _finding("MISSING_ARTIFACT", "mandatory artifact is missing or not a regular file", relative_text)
    tracked = _git(root, "ls-files", "--error-unmatch", "--", relative.as_posix(), check=False)
    if tracked.returncode != 0:
        return None, _finding("UNTRACKED_ARTIFACT", "mandatory artifact is not tracked", relative_text)
    return candidate, None


def _manifest_findings(data: Any) -> list[dict[str, str]]:
    path = MANIFEST_PATH.as_posix()
    if not isinstance(data, dict):
        return [_finding("MANIFEST_STRUCTURE", "manifest root must be an object", path)]
    findings: list[dict[str, str]] = []
    if data.get("schema_version") != "hermes.bootstrap-closure-manifest/1.0":
        findings.append(_finding("MANIFEST_SCHEMA", "invalid schema_version", path))
    if data.get("repository") != {"full_name": "neoengine-ai-org/hermes-agent", "default_branch": "main"}:
        findings.append(_finding("MANIFEST_REPOSITORY", "repository identity is invalid", path))
    if data.get("result_state") != READY:
        findings.append(_finding("MANIFEST_RESULT", "result state cannot be weakened", path))
    if data.get("project") != EXPECTED_PROJECT:
        findings.append(_finding("MANIFEST_PROJECT", "package metadata contract is invalid", path))
    if data.get("console_entrypoints") != EXPECTED_ENTRYPOINTS:
        findings.append(_finding("MANIFEST_ENTRYPOINTS", "console entrypoints are invalid", path))
    expected_artifacts = [
        {"dependency_id": dependency_id, "dependency_class": "ORG_NATIVE_BOOTSTRAP", "path": artifact_path}
        for dependency_id, artifact_path in EXPECTED_ARTIFACTS
    ]
    if data.get("required_artifacts") != expected_artifacts:
        findings.append(_finding("MANIFEST_PROTECTED_SET", "required_artifacts must exactly match the validator-owned set", path))
    if data.get("receipt_provenance") != EXPECTED_PROVENANCE:
        findings.append(_finding("MANIFEST_PROVENANCE", "receipt provenance contract is invalid", path))
    if data.get("optional_surfaces") != EXPECTED_OPTIONAL_SURFACES:
        findings.append(_finding("MANIFEST_OPTIONAL", "optional surface classification is invalid", path))
    if data.get("receipt") != EXPECTED_RECEIPT:
        findings.append(_finding("MANIFEST_RECEIPT", "receipt contract is invalid", path))
    non_claims = data.get("non_claims")
    if (
        not isinstance(non_claims, list)
        or any(not isinstance(item, str) for item in non_claims)
        or len(non_claims) != len(set(non_claims))
        or set(non_claims) != EXPECTED_NON_CLAIMS
    ):
        findings.append(_finding("MANIFEST_NON_CLAIMS", "protected non-claims are incomplete", path))
    if set(data) != {
        "schema_version",
        "repository",
        "result_state",
        "project",
        "console_entrypoints",
        "required_artifacts",
        "receipt_provenance",
        "optional_surfaces",
        "receipt",
        "non_claims",
    }:
        findings.append(_finding("MANIFEST_FIELDS", "manifest contains missing or unknown fields", path))
    return findings


def interpreter_provenance(
    root: Path,
    executable: Path,
    *,
    receipt_venv: Path | None = None,
) -> tuple[dict[str, str], dict[str, str] | None]:
    root = root.resolve()
    lexical_executable = Path(os.path.abspath(executable))
    try:
        executable_venv = lexical_executable.parent.parent.resolve(strict=True)
        venv_relative = executable_venv.relative_to(root)
    except (OSError, ValueError):
        return (
            {"classification": "ASSEMBLED_WORKSPACE_ONLY", "path": "external-interpreter"},
            _finding("SHARED_VENV", "receipt mode requires a repository-local interpreter", str(lexical_executable)),
        )
    if lexical_executable.parent.name != "bin":
        return (
            {"classification": "ASSEMBLED_WORKSPACE_ONLY", "path": "external-interpreter"},
            _finding("SHARED_VENV", "receipt interpreter must use a virtualenv bin directory", str(lexical_executable)),
        )
    allowed_roots = [root / item for item in EXPECTED_PROVENANCE["accepted_repo_local_venvs"]]
    if receipt_venv is not None:
        requested = receipt_venv if receipt_venv.is_absolute() else root / receipt_venv
        requested = Path(os.path.abspath(requested))
        try:
            requested_resolved = requested.resolve(strict=True)
            requested_resolved.relative_to(root)
        except (OSError, ValueError):
            return (
                {"classification": "ASSEMBLED_WORKSPACE_ONLY", "path": "external-interpreter"},
                _finding("SHARED_VENV", "explicit receipt venv escapes the checkout", str(requested)),
            )
        accepted_resolved = set()
        for allowed in allowed_roots:
            try:
                accepted_resolved.add(allowed.resolve(strict=True))
            except OSError:
                continue
        if requested_resolved not in accepted_resolved:
            return (
                {"classification": "ASSEMBLED_WORKSPACE_ONLY", "path": "external-interpreter"},
                _finding("SHARED_VENV", "explicit receipt venv is not in the protected allowlist", str(requested)),
            )
        allowed_roots = [requested_resolved]
    for allowed in allowed_roots:
        if receipt_venv is not None:
            allowed_resolved = requested_resolved
        else:
            try:
                allowed_resolved = allowed.resolve(strict=True)
            except OSError:
                continue
        if executable_venv != allowed_resolved:
            continue
        try:
            allowed_relative = allowed_resolved.relative_to(root)
        except (OSError, ValueError):
            return (
                {"classification": "ASSEMBLED_WORKSPACE_ONLY", "path": "external-interpreter"},
                _finding("SHARED_VENV", "receipt venv is missing or resolves outside the checkout", str(allowed)),
            )
        if allowed_resolved == root or allowed.is_symlink() or not (allowed_resolved / "pyvenv.cfg").is_file():
            return (
                {"classification": "ASSEMBLED_WORKSPACE_ONLY", "path": "external-interpreter"},
                _finding("SHARED_VENV", "receipt interpreter is not inside a repository-local virtualenv", str(allowed)),
            )
        relative = allowed_relative / "bin" / lexical_executable.name
        return {
            "classification": "ORG_NATIVE_BOOTSTRAP",
            "path": relative.as_posix(),
            "venv": venv_relative.as_posix(),
            "python_version": ".".join(str(part) for part in sys.version_info[:3]),
        }, None
    return (
        {"classification": "ASSEMBLED_WORKSPACE_ONLY", "path": "external-interpreter"},
        _finding("SHARED_VENV", "interpreter is not within an approved receipt venv", str(lexical_executable)),
    )


def _lock_findings(root: Path) -> list[dict[str, str]]:
    try:
        result = subprocess.run(
            ["uv", "lock", "--check"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "UV_NO_PROGRESS": "1"},
        )
    except OSError as error:
        return [_finding("LOCK_TOOL_UNAVAILABLE", str(error), "uv.lock")]
    if result.returncode != 0:
        return [_finding("LOCK_DRIFT", (result.stderr or result.stdout).strip(), "uv.lock")]
    return []


def _import_smoke(root: Path, interpreter: Path) -> tuple[dict[str, Any], dict[str, str] | None]:
    probe = r'''
import builtins
import importlib
import json
import os
import socket
import sys

blocked = set(json.loads(os.environ["HERMES_BOOTSTRAP_OPTIONAL_ROOTS"]))
attempted = []
original_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".", 1)[0]
    if root in blocked:
        attempted.append(name)
        raise ImportError(f"optional import blocked during bootstrap: {name}")
    return original_import(name, globals, locals, fromlist, level)

class NetworkBlockedSocket(socket.socket):
    def connect(self, *args, **kwargs):
        raise RuntimeError("network disabled during bootstrap")

def network_blocked(*args, **kwargs):
    raise RuntimeError("network disabled during bootstrap")

builtins.__import__ = guarded_import

class OptionalImportBlocker:
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".", 1)[0]
        if root in blocked:
            attempted.append(fullname)
            raise ImportError(f"optional import blocked during bootstrap: {fullname}")
        return None

sys.meta_path.insert(0, OptionalImportBlocker())
socket.socket = NetworkBlockedSocket
socket.create_connection = network_blocked

targets = json.loads(os.environ["HERMES_BOOTSTRAP_ENTRYPOINTS"])
loaded = []
for command, target in sorted(targets.items()):
    module_name, attribute = target.split(":", 1)
    module = importlib.import_module(module_name)
    value = getattr(module, attribute)
    if not callable(value):
        raise TypeError(f"entrypoint is not callable: {target}")
    loaded.append(command)

from hermes_constants import get_hermes_home
expected_home = os.environ["HERMES_HOME"]
if str(get_hermes_home()) != expected_home:
    raise RuntimeError("Hermes home did not remain isolated")
unexpected_loaded = sorted(root for root in blocked if root in sys.modules)
if unexpected_loaded:
    raise RuntimeError("optional imports were loaded: " + ",".join(unexpected_loaded))
print("HERMES_BOOTSTRAP_PROBE=" + json.dumps({
    "entrypoints": loaded,
    "home": "isolated",
    "network": "disabled",
    "optional_imports_blocked": sorted(set(attempted)),
}, sort_keys=True))
'''
    with tempfile.TemporaryDirectory(prefix="hermes-bootstrap-home-") as home:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": home,
            "HERMES_HOME": str(Path(home) / ".hermes"),
            "TZ": "UTC",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HERMES_BOOTSTRAP_ENTRYPOINTS": json.dumps(EXPECTED_ENTRYPOINTS, sort_keys=True),
            "HERMES_BOOTSTRAP_OPTIONAL_ROOTS": json.dumps(sorted(OPTIONAL_IMPORT_ROOTS)),
        }
        result = subprocess.run(
            [str(interpreter), "-c", probe],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    if result.returncode != 0:
        return {}, _finding("IMPORT_SMOKE", (result.stderr or result.stdout).strip(), "pyproject.toml")
    marker = next(
        (line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith("HERMES_BOOTSTRAP_PROBE=")),
        None,
    )
    if marker is None:
        return {}, _finding("IMPORT_SMOKE", "smoke probe did not emit its terminal record", "pyproject.toml")
    return json.loads(marker), None


def validate(
    root_input: Path,
    *,
    receipt_mode: bool = True,
    receipt_venv: Path | None = None,
    interpreter: Path | None = None,
    run_lock_check: bool = True,
    run_import_smoke: bool = True,
) -> dict[str, Any]:
    root = root_input.resolve()
    interpreter = interpreter or Path(sys.executable)
    findings: list[dict[str, str]] = []
    artifacts: list[dict[str, Any]] = []
    source: dict[str, str] = {}
    try:
        top = Path(_git(root, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
        if top != root:
            findings.append(_finding("ROOT_MISMATCH", "selected root is not the Git top level", str(root)))
        else:
            source = {
                "head": _git(root, "rev-parse", "HEAD").stdout.strip(),
                "tree": _git(root, "rev-parse", "HEAD^{tree}").stdout.strip(),
            }
    except (OSError, subprocess.CalledProcessError) as error:
        findings.append(_finding("ROOT_NOT_GIT", f"selected root is not a Git checkout: {error}", str(root)))

    manifest_file, manifest_error = _contained_regular_file(root, MANIFEST_PATH.as_posix())
    if manifest_error:
        findings.append(manifest_error)
    elif manifest_file is not None:
        try:
            manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            findings.append(_finding("MANIFEST_MALFORMED", str(error), MANIFEST_PATH.as_posix()))
        else:
            findings.extend(_manifest_findings(manifest_data))

    if not findings or not any(item["code"].startswith("ROOT_") for item in findings):
        for dependency_id, relative in EXPECTED_ARTIFACTS:
            artifact, error = _contained_regular_file(root, relative)
            if error:
                findings.append(error)
                continue
            assert artifact is not None
            if relative in {"scripts/run_tests.sh", "scripts/run_tests_v1.sh"}:
                index_entry = _git(root, "ls-files", "-s", "--", relative).stdout.strip()
                index_mode = index_entry.split(maxsplit=1)[0] if index_entry else ""
                if index_mode != "100755":
                    findings.append(
                        _finding(
                            "ARTIFACT_MODE",
                            "test runner must have executable mode 100755 in the Git index",
                            relative,
                        )
                    )
                    continue
            _blob_sha, head_bytes = _head_blob(root, relative)
            worktree_bytes = artifact.read_bytes()
            if worktree_bytes != head_bytes:
                findings.append(
                    _finding(
                        "ARTIFACT_HEAD_MISMATCH",
                        "protected artifact bytes differ from the attested HEAD blob",
                        relative,
                    )
                )
                continue
            artifacts.append(
                {
                    "dependency_id": dependency_id,
                    "dependency_class": "ORG_NATIVE_BOOTSTRAP",
                    "canonical_path_or_provider": relative,
                    "local_required": True,
                    "sha256": _sha256(artifact),
                    "status": "READY",
                }
            )

    if not findings:
        try:
            project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as error:
            findings.append(_finding("PACKAGE_METADATA", str(error), "pyproject.toml"))
        else:
            if project.get("name") != EXPECTED_PROJECT["name"]:
                findings.append(_finding("PACKAGE_NAME", "project.name must be hermes-agent", "pyproject.toml"))
            if project.get("requires-python") != EXPECTED_PROJECT["python_requires"]:
                findings.append(_finding("PYTHON_FLOOR", "requires-python must equal >=3.11", "pyproject.toml"))
            if project.get("scripts") != EXPECTED_ENTRYPOINTS:
                findings.append(_finding("ENTRYPOINTS", "console entrypoints differ from the protected set", "pyproject.toml"))

    provenance: dict[str, str]
    if receipt_mode:
        provenance, provenance_error = interpreter_provenance(root, interpreter, receipt_venv=receipt_venv)
        if provenance_error:
            findings.append(provenance_error)
        plugins = {item.strip() for item in os.environ.get("PYTEST_PLUGINS", "").split(",")}
        if "pytest_live_guard" in plugins or "pytest_live_guard" in sys.modules:
            findings.append(_finding("HOME_PLUGIN", "receipt mode rejects the home pytest plugin"))
        cleanliness = _git(root, "status", "--porcelain=v1", "--untracked-files=all", check=False)
        if cleanliness.returncode != 0 or cleanliness.stdout:
            findings.append(_finding("DIRTY_CHECKOUT", "receipt-grade validation requires a clean checkout"))
    else:
        provenance = {
            "classification": "ASSEMBLED_WORKSPACE_ONLY",
            "path": str(interpreter),
            "mode": "developer_non_receipt",
            "python_version": ".".join(str(part) for part in sys.version_info[:3]),
        }

    if not findings and run_lock_check:
        findings.extend(_lock_findings(root))
    smoke: dict[str, Any] = {}
    if not findings and run_import_smoke:
        smoke, smoke_error = _import_smoke(root, interpreter)
        if smoke_error:
            findings.append(smoke_error)
    lock_path = root / "uv.lock"
    if lock_path.is_file() and not lock_path.is_symlink():
        source["lock_sha256"] = _sha256(lock_path)

    codes = {item["code"] for item in findings}
    if "SHARED_VENV" in codes or "HOME_PLUGIN" in codes:
        state = SHARED_VENV
    elif any(code.startswith("MANIFEST_") for code in codes):
        state = MANIFEST_INVALID
    elif findings:
        state = BLOCKED
    else:
        state = READY
    return {
        "schema_version": "hermes.bootstrap-validation/1.0",
        "validator_version": VERSION,
        "root": str(root),
        "source": source,
        "state": state,
        "optional_provider_state": OPTIONAL_UNAVAILABLE,
        "receipt_mode": receipt_mode,
        "interpreter": provenance,
        "environment": {
            "provider_credentials": "CLEARED_FOR_SMOKE",
            "network": "DISABLED_FOR_SMOKE",
            "home_pytest_plugin": "DISABLED" if receipt_mode else "DEVELOPER_POLICY",
        },
        "smoke": smoke,
        "artifacts": artifacts,
        "findings": sorted(findings, key=lambda item: (item["code"], item.get("path", ""), item["message"])),
    }


def build_receipt(root: Path, report: dict[str, Any]) -> dict[str, Any]:
    if report.get("state") != READY or report.get("receipt_mode") is not True:
        raise ValueError("receipt emission requires a receipt-mode ready report")
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    tree = _git(root, "rev-parse", "HEAD^{tree}").stdout.strip()
    _assert_receipt_snapshot(root, report, head, tree)
    dependencies = []
    for artifact in report["artifacts"]:
        item = dict(artifact)
        blob_sha, blob = _head_blob(root, item["canonical_path_or_provider"])
        if hashlib.sha256(blob).hexdigest() != item["sha256"]:
            raise ValueError("receipt artifact does not match the validated HEAD blob")
        item["digest"] = item.pop("sha256")
        item["version"] = "1"
        item["blob_sha"] = blob_sha
        dependencies.append(item)
    manifest_blob_sha, manifest_blob = _head_blob(root, MANIFEST_PATH.as_posix())
    validator_blob_sha, validator_blob = _head_blob(root, VALIDATOR_PATH.as_posix())
    payload: dict[str, Any] = {
        "schema_version": "hermes.bootstrap-closure-receipt/1.0",
        "emitter_repository": {
            "full_name": "neoengine-ai-org/hermes-agent",
            "default_branch": "main",
        },
        "source": report["source"],
        "manifest": {
            "path": MANIFEST_PATH.as_posix(),
            "blob_sha": manifest_blob_sha,
            "sha256": hashlib.sha256(manifest_blob).hexdigest(),
            "schema_version": "hermes.bootstrap-closure-manifest/1.0",
        },
        "validator": {
            "path": VALIDATOR_PATH.as_posix(),
            "blob_sha": validator_blob_sha,
            "sha256": hashlib.sha256(validator_blob).hexdigest(),
            "version": VERSION,
        },
        "dependencies": dependencies,
        "interpreter": report["interpreter"],
        "environment": report["environment"],
        "proofs": {
            "clean_checkout": "required_by_ci_and_detached_proof",
            "isolated_smoke": report["smoke"],
            "admission_points": ["pull_request", "main_push", "manual_checkout_validation"],
            "negative_controls": [
                "tracked_contained_non_symlink_artifacts",
                "locked_package_metadata",
                "credential_free_network_disabled_imports",
                "shared_home_venv_non_receipt",
                "home_pytest_plugin_disabled",
                "optional_provider_imports_deferred",
            ],
        },
        "result_state": READY,
        "coverage": "candidate_checkout",
        "typed_omissions": [
            "protected_main_live_verification",
            "ci_run_identity",
            "independent_exact_head_review",
        ],
        "rollback": "Revert the bootstrap closure commit; developer shared-venv mode remains non-receipt.",
        "non_claims": sorted(EXPECTED_NON_CLAIMS),
    }
    digest = _canonical_digest(payload)
    receipt = dict(payload)
    receipt["receipt_id"] = f"hermes-bootstrap-{digest[:20]}"
    receipt["canonical_payload_digest"] = digest
    _assert_receipt_snapshot(root, report, head, tree)
    return receipt


def _assert_receipt_snapshot(root: Path, report: dict[str, Any], head: str, tree: str) -> None:
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout:
        raise ValueError("receipt emission requires a clean checkout")
    if report.get("source", {}).get("head") != head or report.get("source", {}).get("tree") != tree:
        raise ValueError("receipt source differs from the validated checkout")
    if _git(root, "rev-parse", "HEAD").stdout.strip() != head or _git(root, "rev-parse", "HEAD^{tree}").stdout.strip() != tree:
        raise ValueError("receipt source changed during emission")
    for artifact in report.get("artifacts", []):
        _blob_sha, blob = _head_blob(root, artifact["canonical_path_or_provider"])
        if hashlib.sha256(blob).hexdigest() != artifact["sha256"]:
            raise ValueError("receipt artifact changed after validation")


def _write_receipt(root: Path, relative_text: str, receipt: dict[str, Any]) -> None:
    if relative_text != "artifacts/bootstrap/hermes-bootstrap-closure-receipt-v1.json":
        raise ValueError("receipt output path differs from the product-owned contract")
    pure = PurePosixPath(relative_text)
    if pure.is_absolute() or ".." in pure.parts or pure == PurePosixPath("."):
        raise ValueError("receipt output must be checkout-relative")
    output = root.joinpath(*pure.parts)
    probe = root
    for part in Path(*pure.parts).parent.parts:
        probe = probe / part
        if probe.is_symlink():
            raise ValueError("receipt output parent contains a symlink")
    output.resolve(strict=False).relative_to(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and (output.is_symlink() or not output.is_file()):
        raise ValueError("receipt output target is not a regular file")
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--developer-mode", action="store_true", help="allow non-receipt developer interpreter provenance")
    parser.add_argument("--receipt-venv", type=Path, help="explicit repository-local venv admitted for this receipt run")
    parser.add_argument("--receipt-out", help="checkout-relative output path; written only on receipt-grade PASS")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    report = validate(
        args.root,
        receipt_mode=not args.developer_mode,
        receipt_venv=args.receipt_venv,
    )
    if report["state"] == READY and args.receipt_out:
        try:
            receipt = build_receipt(Path(report["root"]), report)
            _write_receipt(Path(report["root"]), args.receipt_out, receipt)
            report["receipt"] = {"path": args.receipt_out, "receipt_id": receipt["receipt_id"]}
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            report["state"] = BLOCKED
            report["findings"].append(_finding("RECEIPT_OUTPUT", str(error), args.receipt_out))
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["state"])
        print(report["optional_provider_state"])
        print(f"interpreter={report['interpreter']['path']}")
        if report["source"]:
            print(
                f"source_head={report['source']['head']} "
                f"source_tree={report['source']['tree']} "
                f"lock_sha256={report['source'].get('lock_sha256', 'unavailable')}"
            )
        for item in report["findings"]:
            print(f"{item['code']}: {item['message']}", file=sys.stderr)
        if "receipt" in report:
            print(f"receipt={report['receipt']['path']} id={report['receipt']['receipt_id']}")
    return 0 if report["state"] == READY else 1


if __name__ == "__main__":
    raise SystemExit(main())
