#!/usr/bin/env python3
"""Prove that mandatory bootstrap dependencies live in this exact checkout."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = "org-bootstrap-closure/1.0.0"
ALL_CLASSES = {
    "ORG_NATIVE_BOOTSTRAP",
    "SHARED_PLATFORM_CONTRACT",
    "CHILD_OR_PROVIDER_OPTIONAL",
    "ASSEMBLED_WORKSPACE_ONLY",
}
LOCAL_CLASSES = {"ORG_NATIVE_BOOTSTRAP", "SHARED_PLATFORM_CONTRACT"}
OPTIONAL_CLASSES = ALL_CLASSES - LOCAL_CLASSES
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class Blocked(RuntimeError):
    """Raised when checkout-local bootstrap proof must fail closed."""


def git(root: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode:
        raise Blocked(f"git {' '.join(args)} failed: {process.stderr.strip()}")
    return process.stdout.strip()


def repository_root(raw: str | None) -> Path:
    root = (Path(raw).expanduser() if raw else Path(__file__).resolve().parents[1]).resolve()
    top = Path(git(root, "rev-parse", "--show-toplevel")).resolve()
    if root != top:
        raise Blocked(f"root must be Git top level: {root} != {top}")
    return root


def relative_path(raw: Any, label: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise Blocked(f"{label} must be non-empty")
    value = raw.strip().replace("\\", "/")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value.startswith("~")
        or ".." in path.parts
        or path.as_posix() in {"", "."}
    ):
        raise Blocked(f"{label} escapes checkout: {raw!r}")
    return path.as_posix()


def tracked_file(root: Path, raw: Any, label: str) -> Path:
    relative = relative_path(raw, label)
    path = root
    for part in PurePosixPath(relative).parts:
        path /= part
        if path.is_symlink():
            raise Blocked(f"{label} contains symlink: {relative}")
    if not path.is_file():
        raise Blocked(f"{label} missing or not regular: {relative}")
    git(root, "ls-files", "--error-unmatch", "--", relative)
    try:
        path.resolve().relative_to(root)
    except ValueError as error:
        raise Blocked(f"{label} resolves outside checkout: {relative}") from error
    return path


def skill_frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise Blocked(f"missing skill frontmatter: {path}")
    result: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return result
        if ":" in line and not line.lstrip().startswith("#"):
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip().strip("\"'")
    raise Blocked(f"unterminated skill frontmatter: {path}")


def stable_version(raw: str, label: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(raw)
    if not match:
        raise Blocked(f"{label} must be stable SemVer")
    return tuple(map(int, match.groups()))


def walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def validate(root_raw: str | None, manifest_raw: str | None) -> dict[str, Any]:
    root = repository_root(root_raw)
    manifest_relative = relative_path(
        manifest_raw or "config/org-bootstrap-closure-v1.json",
        "manifest",
    )
    manifest = json.loads(
        tracked_file(root, manifest_relative, "manifest").read_text(encoding="utf-8")
    )
    if manifest.get("schema_version") != SCHEMA:
        raise Blocked("unsupported manifest schema")

    repository = manifest.get("repository", {})
    name = repository.get("full_name") if isinstance(repository, dict) else None
    if not isinstance(name, str) or "/" not in name:
        raise Blocked("repository.full_name must be owner/name")

    prefix = manifest.get("terminal_state_prefix")
    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", prefix):
        raise Blocked("invalid terminal prefix")
    classes = manifest.get("dependency_classes")
    if not isinstance(classes, list) or set(classes) != ALL_CLASSES:
        raise Blocked("dependency class taxonomy is incomplete")

    validator_relative = Path(__file__).resolve().relative_to(root).as_posix()
    checked: set[str] = set()
    for raw in [manifest_relative, validator_relative, *manifest.get("authority_files", [])]:
        relative = relative_path(raw, "authority file")
        tracked_file(root, relative, "authority file")
        checked.add(relative)

    required_files = manifest.get("required_files")
    if not isinstance(required_files, list) or not required_files:
        raise Blocked("required_files must be non-empty")
    for index, item in enumerate(required_files):
        if not isinstance(item, dict) or item.get("class") not in LOCAL_CLASSES:
            raise Blocked(f"invalid required_files[{index}]")
        relative = relative_path(item.get("path"), f"required_files[{index}].path")
        tracked_file(root, relative, "required file")
        checked.add(relative)

    skills: list[dict[str, Any]] = []
    for index, item in enumerate(manifest.get("required_skills", [])):
        if not isinstance(item, dict) or item.get("class") not in LOCAL_CLASSES:
            raise Blocked(f"invalid required_skills[{index}]")
        skill_id = item.get("id")
        relative = relative_path(
            item.get("canonical_path"),
            f"required_skills[{index}].canonical_path",
        )
        source_path = tracked_file(root, relative, "required skill")
        metadata = skill_frontmatter(source_path)
        if metadata.get("name") != skill_id:
            raise Blocked(f"skill identity mismatch: {relative}")
        minimum = item.get("minimum_version")
        observed = metadata.get("version")
        if minimum is not None:
            if (
                not isinstance(minimum, str)
                or not isinstance(observed, str)
                or stable_version(observed, relative) < stable_version(minimum, relative)
            ):
                raise Blocked(f"skill version below minimum: {relative}")
        source = source_path.read_bytes()
        for mirror_raw in item.get("mirrors", []):
            mirror = relative_path(mirror_raw, f"{skill_id}.mirror")
            mirror_path = tracked_file(root, mirror, "skill mirror")
            if mirror_path.read_bytes() != source:
                raise Blocked(f"skill mirror drift: {mirror}")
            checked.add(mirror)
        checked.add(relative)
        skills.append({"id": skill_id, "path": relative, "version": observed})

    bindings = {"examined": 0, "external_optional": 0, "local_runtime": 0}
    for index, item in enumerate(manifest.get("binding_files", [])):
        if not isinstance(item, dict):
            raise Blocked(f"invalid binding_files[{index}]")
        relative = relative_path(item.get("path"), f"binding_files[{index}].path")
        data = json.loads(
            tracked_file(root, relative, "binding file").read_text(encoding="utf-8")
        )
        checked.add(relative)
        for entry in walk_json(data):
            canonical = entry.get("canonical_path")
            if not isinstance(canonical, str) or not canonical.strip():
                continue
            bindings["examined"] += 1
            if entry.get("required_at_bootstrap") is True or entry.get("runtime_executable") is True:
                local = entry.get("local_path") or entry.get("vendored_path")
                if not isinstance(local, str) or not local.strip():
                    raise Blocked(
                        f"external executable/bootstrap binding lacks local path: {canonical}"
                    )
                tracked_file(root, local, "binding local path")
                bindings["local_runtime"] += 1
            else:
                bindings["external_optional"] += 1

    for index, item in enumerate(manifest.get("declared_optional_external_references", [])):
        if (
            not isinstance(item, dict)
            or item.get("class") not in OPTIONAL_CLASSES
            or item.get("required_at_bootstrap") is not False
        ):
            raise Blocked(f"invalid optional reference {index}")

    return {
        "repository": name,
        "head": git(root, "rev-parse", "HEAD"),
        "manifest": manifest_relative,
        "checked_paths": sorted(checked),
        "required_skills": skills,
        "bindings": bindings,
        "state": f"{prefix}_BOOTSTRAP_CLOSURE_READY",
    }


def self_test() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="bootstrap-closure-") as temporary:
        root = Path(temporary)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        git(root, "config", "user.email", "test@example.invalid")
        git(root, "config", "user.name", "Test")
        (root / "config").mkdir()
        (root / "scripts").mkdir()
        (root / "skills/x").mkdir(parents=True)
        script = root / "scripts/validate-org-bootstrap-closure.py"
        script.write_text(
            Path(__file__).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (root / "AGENTS.md").write_text("# x\n", encoding="utf-8")
        (root / "skills/x/SKILL.md").write_text(
            "---\nname: x\nversion: 1.0.0\n---\n",
            encoding="utf-8",
        )
        manifest = {
            "schema_version": SCHEMA,
            "repository": {"full_name": "x/y"},
            "terminal_state_prefix": "TEST",
            "dependency_classes": sorted(ALL_CLASSES),
            "authority_files": [],
            "required_files": [
                {"path": "AGENTS.md", "class": "ORG_NATIVE_BOOTSTRAP"}
            ],
            "required_skills": [
                {
                    "id": "x",
                    "class": "ORG_NATIVE_BOOTSTRAP",
                    "canonical_path": "skills/x/SKILL.md",
                    "minimum_version": "1.0.0",
                    "mirrors": [],
                }
            ],
            "binding_files": [],
            "declared_optional_external_references": [],
        }
        manifest_path = root / "config/org-bootstrap-closure-v1.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-qm", "valid"],
            check=True,
        )

        def run_validator() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, str(script), "--root", str(root), "--json"],
                capture_output=True,
                text=True,
                check=False,
            )

        if run_validator().returncode:
            raise Blocked("valid self-test fixture failed")
        (root / "AGENTS.md").unlink()
        if run_validator().returncode == 0:
            raise Blocked("missing-file self-test passed")
        subprocess.run(
            ["git", "-C", str(root), "checkout", "--", "AGENTS.md"],
            check=True,
        )
        (root / "bindings.json").write_text(
            json.dumps(
                {
                    "bindings": [
                        {"canonical_path": "../x", "runtime_executable": True}
                    ]
                }
            ),
            encoding="utf-8",
        )
        manifest["binding_files"] = [{"path": "bindings.json"}]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-qm", "negative"],
            check=True,
        )
        if run_validator().returncode == 0:
            raise Blocked("external-runtime self-test passed")
    return {
        "state": "ORG_BOOTSTRAP_CLOSURE_SELF_TEST_PASS",
        "cases": ["valid", "missing_file", "external_runtime"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root")
    parser.add_argument("--manifest")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    arguments = parser.parse_args()
    try:
        output = (
            self_test()
            if arguments.self_test
            else validate(arguments.root, arguments.manifest)
        )
    except (Blocked, OSError, ValueError, json.JSONDecodeError) as error:
        output = {"state": "ORG_BOOTSTRAP_CLOSURE_BLOCKED", "errors": [str(error)]}
        print(json.dumps(output) if arguments.json else output["state"])
        return 1
    print(json.dumps(output, sort_keys=True) if arguments.json else output["state"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
