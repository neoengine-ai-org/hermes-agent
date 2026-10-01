#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import contextlib
import fnmatch
import functools
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

_SCRIPT = Path(os.path.abspath(__file__))
# Refuse to run through a symlink inside the repository (the script, scripts/ci
# or scripts): resolve() would follow it and load the classifier and policy
# from an unpinned location.
if any(part.is_symlink() for part in (_SCRIPT, _SCRIPT.parent, _SCRIPT.parent.parent)):
    raise SystemExit("runtime_os_adapter must not run through a symlinked path")
TRUST_ROOT = _SCRIPT.parents[2].resolve()
CANDIDATE_ROOT = Path(os.environ.get("RUNTIME_OS_CANDIDATE_ROOT", TRUST_ROOT)).resolve()
LOCK_PATH = TRUST_ROOT / "ci/runtime-os/policy-bundle.lock.json"
EXPECTED_POLICY_VERSION = "2.1.0"
EXPECTED_SOURCE_COMMIT = "871e416afc55db187d2b6f29c9ff7cac96472223"
EXPECTED_POLICY_DIGEST = "1bdb16a0322fb654b519b49e4608d6d9f369fa1572ac1901a596605262525b19"
EXPECTED_PARITY_FIXTURE_DIGEST = "ed3f140b8324c746791173a084e4a6ea7bedb2e6e27c3eb9079cb5d194f708dd"
EXPECTED_CONTEXTS = ["Hermes CI required", "Review evidence required", "Merge admission"]
TEST_SUFFIXES = {".py"}
EXECUTABLE_NAMES = {"Dockerfile", "Makefile"}


def load_policy() -> dict[str, Any]:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    bundle_path = TRUST_ROOT / lock["bundle"]
    payload = bundle_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if lock["digest"] != EXPECTED_POLICY_DIGEST or digest != EXPECTED_POLICY_DIGEST:
        raise ValueError(f"policy digest mismatch: expected {EXPECTED_POLICY_DIGEST}, got {digest}")
    policy = json.loads(payload)
    required = {
        "policy_version": EXPECTED_POLICY_VERSION,
        "source_commit": EXPECTED_SOURCE_COMMIT,
        "stable_contexts": EXPECTED_CONTEXTS,
        "slice_count": 6,
        "mode": "advisory",
        "candidate_may_rewrite_policy": False,
        "private_cross_repo_checkout": False,
        "telemetry_source": "protected_main_only",
        "canonical_decision_contract": {
            "proof_selection_digest": "sha256:fb41392b598faa4ab442f305e4a6dff61811d53cd5046a388219d09de4d41e2b",
            "proof_selection_test_digest": "sha256:3a2b7bd064f161821773aac1ef2eedcdf2afde7215f3ce51df585fe7427211d9",
            "review_routing_digest": "sha256:5a9220bb1c5b741f0607ec33213c0632db25533608d1ee1bf8b19eefb7ce92a5",
            "review_routing_test_digest": "sha256:18a1ac74525f8a622dbd3144cb7bac8262c4d6c6f271ed5c84b89b54309a9c8a",
            "types_digest": "sha256:cb76164215f07a4e64e0cc442adf5b6c44deec93d6b3c796ec33164f113dc71b",
        },
        "repository_profile": {
            "capability_classes": [
                "deterministic_unit",
                "integration_e2e",
                "review_evidence",
                "merge_admission",
            ],
            "full_proof_events": [
                "merge_group",
                "push",
                "schedule",
                "workflow_dispatch",
            ],
            "id": "hermes-agent",
            "narrow_selection": "transitive_python_import_closure",
            "parity_fixtures": "ci/runtime-os/hermes-parity-fixtures.v1.json",
            "unknown_impact": "full_proof",
            "version": 1,
        },
    }
    for key, expected in required.items():
        if policy.get(key) != expected:
            raise ValueError(f"invalid policy field {key}: expected {expected!r}")
    fixture_path = TRUST_ROOT / policy["repository_profile"]["parity_fixtures"]
    fixture_digest = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    if fixture_digest != EXPECTED_PARITY_FIXTURE_DIGEST:
        raise ValueError("repository parity fixture digest mismatch")
    if lock["policy_version"] != EXPECTED_POLICY_VERSION or lock["source_commit"] != EXPECTED_SOURCE_COMMIT:
        raise ValueError("policy lock identity mismatch")
    return policy


def load_classifier() -> Any:
    path = TRUST_ROOT / "scripts/ci_risk_classifier.py"
    spec = importlib.util.spec_from_file_location("_runtime_os_trusted_classifier", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load trusted classifier")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.SELF_CHANGE_PATHS.add("scripts/ci/runtime_os_adapter.py")
    return module


def full_proof(files: list[str], event_name: str, policy: dict[str, Any]) -> tuple[bool, str]:
    if event_name in set(policy["repository_profile"]["full_proof_events"]):
        return True, f"{event_name}_requires_full_proof"
    triggers = policy["full_proof_triggers"]
    for path in files:
        normalized = path.replace("\\", "/").removeprefix("./")
        if any(normalized == trigger or normalized.startswith(trigger) for trigger in triggers):
            return True, f"full_proof_trigger:{normalized}"
    return False, "narrow_change"


# Parallel test processes (and ``hermes update``) create and delete bytecode
# caches in the shared checkout at any moment. A ``__pycache__`` directory is
# still walked (a tracked ``*.py`` inside one is discovered), but if it or an
# entry inside it changes mid-walk the whole walk restarts, so a successful
# discovery always describes one complete, consistent listing of the tree.
_BYTECODE_CACHE = "__pycache__"
_WALK_ATTEMPTS = 5
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)

# Candidate root -> (st_dev, st_ino) bound the first time it is opened; every
# later open must reach that same directory.
_ROOT_IDENTITY: dict[Path, tuple[int, int]] = {}
# (root, relative path) -> (st_dev, st_ino) of every regular file the latest
# successful discovery returned; reads verify they open that same file.
_DISCOVERED_IDENTITY: dict[tuple[Path, str], tuple[int, int]] = {}


class _CacheChurn(Exception):
    """A bytecode cache changed during the walk; the walk restarts."""


def _in_bytecode_cache(parts: tuple[str, ...]) -> bool:
    return _BYTECODE_CACHE in parts


def _open_nofollow(name: str, flags: int, dir_fd: int | None, label: str) -> int:
    """``os.open`` relative to ``dir_fd`` that refuses a symlink (``ValueError``)."""
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError as error:
        try:
            is_link = stat.S_ISLNK(os.lstat(name, dir_fd=dir_fd).st_mode)
        except OSError:
            is_link = False
        if is_link:
            raise ValueError(f"runtime-OS discovery refuses symlink: {label}") from error
        raise


def _open_root(root: Path) -> int:
    """Open the candidate root through every component, binding its identity.

    Each component from ``/`` is opened with ``O_NOFOLLOW`` relative to its
    parent's descriptor, so an ancestor replaced by a symlink raises; the
    opened root must also be the directory bound on first use, so an ancestor
    replaced by a different real directory raises too.
    """
    if not root.is_absolute():
        raise ValueError(f"candidate root must be absolute: {root}")
    directory_fd = os.open(root.anchor, _DIRECTORY_FLAGS)
    try:
        for name in root.parts[1:]:
            child_fd = _open_nofollow(name, _DIRECTORY_FLAGS, directory_fd, str(root))
            os.close(directory_fd)
            directory_fd = child_fd
        opened = os.fstat(directory_fd)
        identity = (opened.st_dev, opened.st_ino)
        if _ROOT_IDENTITY.setdefault(root, identity) != identity:
            raise ValueError(f"candidate root changed after it was bound: {root}")
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd


def _open_relative(root_fd: int, parts: tuple[str, ...]) -> int:
    """Open the directory ``parts`` below an open root without following symlinks.

    Consumes ``root_fd``; the caller owns the returned descriptor.
    """
    directory_fd = root_fd
    try:
        for index, name in enumerate(parts):
            child_fd = _open_nofollow(name, _DIRECTORY_FLAGS, directory_fd, "/".join(parts[: index + 1]))
            os.close(directory_fd)
            directory_fd = child_fd
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd


def _walk_py_files(start: Path, prune) -> list[Path]:
    """Regular ``*.py`` files under ``start``, failing closed.

    Directories for which ``prune(parts)`` is true (``parts`` relative to
    ``CANDIDATE_ROOT``) are never entered. Every other directory is walked
    through descriptors: the candidate root is anchored by ``_open_root``,
    each child directory is opened with ``O_NOFOLLOW`` relative to its
    parent and must be the same directory that was listed (``d_ino``) and
    classified (``lstat``). A directory symlink or a ``*.py`` symlink raises
    (pytest would collect through it, so skipping it would silently narrow
    selection), as does any listing/classification error, any non-cache
    entry that vanished, and an existing ``start`` that is not a directory.
    A change inside a bytecode cache restarts the walk; persistent churn
    raises. Only a missing ``start`` yields nothing.
    """
    for _ in range(_WALK_ATTEMPTS):
        try:
            return _walk_once(start, prune)
        except _CacheChurn:
            continue
    raise RuntimeError(
        f"bytecode caches under {start} kept changing during discovery; refusing a possibly incomplete universe"
    )


def _walk_once(start: Path, prune) -> list[Path]:
    start_parts = start.relative_to(CANDIDATE_ROOT).parts
    try:
        start_fd = _open_relative(_open_root(CANDIDATE_ROOT), start_parts)
    except FileNotFoundError:
        return []
    found: list[Path] = []
    identities: dict[tuple[Path, str], tuple[int, int]] = {}

    def walk(directory_fd: int, directory: Path, relative: tuple[str, ...]) -> None:
        with os.scandir(directory_fd) as scanner:
            entries = sorted(scanner, key=lambda entry: entry.name)
        for entry in entries:
            parts = relative + (entry.name,)
            label = "/".join(parts)
            try:
                info = os.lstat(entry.name, dir_fd=directory_fd)
            except FileNotFoundError:
                if _in_bytecode_cache(parts):
                    raise _CacheChurn() from None
                raise
            is_python = fnmatch.fnmatchcase(entry.name, "*.py")
            if stat.S_ISLNK(info.st_mode):
                if is_python:
                    raise ValueError(f"runtime-OS discovery refuses symlink: {label}")
                if prune(parts):
                    continue
                try:
                    target_is_directory = stat.S_ISDIR(os.stat(entry.name, dir_fd=directory_fd).st_mode)
                except FileNotFoundError:
                    target_is_directory = False  # dangling non-Python link: nothing to collect
                if target_is_directory:
                    raise ValueError(f"runtime-OS discovery refuses symlink: {label}")
                continue
            if is_python and stat.S_ISREG(info.st_mode):
                found.append(directory / entry.name)
                identities[(CANDIDATE_ROOT, label)] = (info.st_dev, info.st_ino)
            if prune(parts) or not stat.S_ISDIR(info.st_mode):
                continue
            if entry.inode() != info.st_ino:
                raise ValueError(f"{label} changed during discovery")
            try:
                child_fd = _open_nofollow(entry.name, _DIRECTORY_FLAGS, directory_fd, label)
            except FileNotFoundError:
                if _in_bytecode_cache(parts):
                    raise _CacheChurn() from None
                raise
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise ValueError(f"{label} changed during discovery")
                walk(child_fd, directory / entry.name, parts)
            finally:
                os.close(child_fd)

    try:
        walk(start_fd, start, start_parts)
    finally:
        os.close(start_fd)
    _DISCOVERED_IDENTITY.update(identities)
    return found


def discover_tests() -> list[str]:
    skip_parts = {"integration", "e2e", "docker"}
    return sorted(
        str(path.relative_to(CANDIDATE_ROOT))
        for path in _walk_py_files(CANDIDATE_ROOT / "tests", lambda parts: parts[-1] in skip_parts)
        if path.name.startswith("test_")
        and not (set(path.relative_to(CANDIDATE_ROOT).parts) & skip_parts)
    )


def discover_python_sources() -> list[str]:
    excluded = {".git", ".venv", "tests", "venv"}
    # Generated interpreter/environment trees at the repository root are not
    # source: the bootstrap proof venv and the restored CI environment (which
    # carries a whole CPython stdlib under ci-fast/) would otherwise be parsed.
    generated_roots = {".bootstrap-proof-venv", "ci-fast"}

    def prune(parts: tuple[str, ...]) -> bool:
        return parts[-1] in excluded or (len(parts) == 1 and parts[0] in generated_roots)

    return sorted(
        str(path.relative_to(CANDIDATE_ROOT))
        for path in _walk_py_files(CANDIDATE_ROOT, prune)
        if not (set(path.relative_to(CANDIDATE_ROOT).parts) & excluded)
    )


def _module_name(path: str) -> str:
    module = path.removesuffix(".py").replace("/", ".")
    return module.removesuffix(".__init__")


def _iter_nodes(tree: ast.AST):
    """Yield every node of ``tree`` (same node set as ``ast.walk``).

    A plain stack walk avoids ``ast.walk``'s per-node ``iter_child_nodes`` /
    ``iter_fields`` generator overhead, which dominated selection time.
    """
    stack = [tree]
    while stack:
        node = stack.pop()
        yield node
        for field in node._fields:
            value = getattr(node, field, None)
            if isinstance(value, list):
                stack.extend(item for item in value if isinstance(item, ast.AST))
            elif isinstance(value, ast.AST):
                stack.append(value)


def _candidate_stat(root: Path, path: str) -> os.stat_result | None:
    """``lstat`` of ``root/path`` through the anchored no-follow chain.

    ``None`` when it does not exist; a symlink anywhere on the path (the final
    component included) raises ``ValueError``.
    """
    parts = Path(path).parts
    try:
        directory_fd = _open_relative(_open_root(root), parts[:-1])
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        info = os.lstat(parts[-1], dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    finally:
        os.close(directory_fd)
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"runtime-OS discovery refuses symlink: {path}")
    return info


def _read_candidate_bytes(root: Path, path: str, expected: tuple[int, int] | None = None) -> bytes:
    """Read ``root/path`` without following any symlink.

    The root is anchored by ``_open_root``; each directory component and the
    file itself are opened with ``O_NOFOLLOW`` relative to the parent's
    descriptor, and when discovery recorded the file's identity the opened
    file must be that same file.
    """
    parts = Path(path).parts
    if _PLAN_DIRECTORIES is not None and root == CANDIDATE_ROOT:
        file_fd = _open_nofollow(parts[-1], _FILE_FLAGS, _plan_directory(parts[:-1]), path)
    else:
        directory_fd = _open_relative(_open_root(root), parts[:-1])
        try:
            file_fd = _open_nofollow(parts[-1], _FILE_FLAGS, directory_fd, path)
        finally:
            os.close(directory_fd)
    with os.fdopen(file_fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if expected is not None and (opened.st_dev, opened.st_ino) != expected:
            raise ValueError(f"{path} changed after discovery")
        return handle.read()


def _decode_source(data: bytes) -> str:
    """Decode exactly like ``Path.read_text(encoding="utf-8")`` (universal newlines)."""
    return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8").read()


def _read_candidate(root: Path, path: str, expected: tuple[int, int] | None = None) -> str:
    return _decode_source(_read_candidate_bytes(root, path, expected))


# Parse results are cached by the SHA-256 of the exact bytes read, never by
# path or inode: every lookup re-reads the candidate (anchored, no-follow,
# identity-checked), so an in-place rewrite or a replacement can never be
# answered from a stale parse. Within one ``select_tests`` call a snapshot
# keeps each file read once, so a plan sees one consistent version of it.
_REFERENCES_BY_DIGEST: dict[bytes, frozenset[str]] = {}
_PREFIXES_BY_DIGEST: dict[bytes, frozenset[str]] = {}
_PLAN_SNAPSHOT: dict[tuple[Path, str], bytes] | None = None
# Within one plan the candidate root is anchored once, and the most recently
# used directory descriptor is reused (paths arrive grouped by directory), so
# reads stay anchored and no-follow without re-walking from "/" per file.
_PLAN_DIRECTORIES: dict[str, object] | None = None


def _plan_directory(parts: tuple[str, ...]) -> int:
    assert _PLAN_DIRECTORIES is not None
    if "root" not in _PLAN_DIRECTORIES:
        _PLAN_DIRECTORIES["root"] = _open_root(CANDIDATE_ROOT)
    root_fd = _PLAN_DIRECTORIES["root"]
    if not parts:
        return root_fd  # type: ignore[return-value]
    last = _PLAN_DIRECTORIES.get("last")
    if last is not None and last[0] == parts:  # type: ignore[index]
        return last[1]  # type: ignore[index]
    if last is not None:
        os.close(last[1])  # type: ignore[index]
        del _PLAN_DIRECTORIES["last"]
    directory_fd = _open_relative(os.dup(root_fd), parts)  # type: ignore[arg-type]
    _PLAN_DIRECTORIES["last"] = (parts, directory_fd)
    return directory_fd


def _candidate_digest(path: str) -> bytes:
    key = (CANDIDATE_ROOT, path)
    if _PLAN_SNAPSHOT is not None and key in _PLAN_SNAPSHOT:
        return _PLAN_SNAPSHOT[key]
    data = _read_candidate_bytes(CANDIDATE_ROOT, path, _DISCOVERED_IDENTITY.get(key))
    digest = hashlib.sha256(data).digest()
    if digest not in _REFERENCES_BY_DIGEST:
        _REFERENCES_BY_DIGEST[digest] = _parse_references(_decode_source(data), path)
    if _PLAN_SNAPSHOT is not None:
        _PLAN_SNAPSHOT[key] = digest
    return digest


def _parse_references(source: str, path: str) -> frozenset[str]:
    tree = ast.parse(source, filename=path)
    references: set[str] = set()
    for node in _iter_nodes(tree):
        if isinstance(node, ast.Import):
            references.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported = node.module or ""
            if imported:
                references.add(imported)
                references.update(
                    f"{imported}.{alias.name}"
                    for alias in node.names
                    if alias.name != "*"
                )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            references.update(
                re.findall(
                    r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b",
                    node.value,
                )
            )
    return frozenset(references)


def _module_references(path: str) -> frozenset[str]:
    return _REFERENCES_BY_DIGEST[_candidate_digest(path)]


def _imports_module(path: str, module_name: str) -> bool:
    """Return whether a source/test directly references a module boundary."""
    return any(
        reference == module_name or reference.startswith(f"{module_name}.")
        for reference in _module_references(path)
    )


def _reference_prefixes(path: str) -> frozenset[str]:
    """Every dotted prefix of every reference: ``a.b.c`` -> ``a``, ``a.b``, ``a.b.c``.

    ``_imports_module(path, m)`` holds exactly when ``m`` is in this set.
    """
    digest = _candidate_digest(path)
    prefixes = _PREFIXES_BY_DIGEST.get(digest)
    if prefixes is None:
        expanded: set[str] = set()
        for reference in _REFERENCES_BY_DIGEST[digest]:
            parts = reference.split(".")
            expanded.update(".".join(parts[: index + 1]) for index in range(len(parts)))
        prefixes = _PREFIXES_BY_DIGEST[digest] = frozenset(expanded)
    return prefixes


def _impacted_closure(changed_module: str) -> tuple[set[str], list[str]]:
    """Transitive importer closure of ``changed_module`` over repository sources.

    Breadth-first over a prefix -> importer index; the same least fixpoint the
    pairwise rescan computed, in time linear in the references. Also returns
    sources that could not be parsed; any such source outside the closure
    marks the plan unknown (unlike the old order-dependent rescan, a parse
    failure that shares its module name with an impacted source does not).
    """
    importers: dict[str, list[str]] = {}
    parse_failures: list[str] = []
    for source_path in discover_python_sources():
        try:
            prefixes = _reference_prefixes(source_path)
        except (OSError, SyntaxError, UnicodeError):
            parse_failures.append(source_path)
            continue
        for prefix in prefixes:
            importers.setdefault(prefix, []).append(source_path)
    impacted = {changed_module}
    queue = [changed_module]
    while queue:
        module = queue.pop()
        for source_path in importers.get(module, ()):
            source_module = _module_name(source_path)
            if source_module not in impacted:
                impacted.add(source_module)
                queue.append(source_module)
    return impacted, parse_failures


@contextlib.contextmanager
def _plan_snapshot():
    """Read each candidate file at most once for the duration of one plan."""
    global _PLAN_SNAPSHOT, _PLAN_DIRECTORIES
    if _PLAN_SNAPSHOT is not None:
        yield  # nested: the outer plan owns the snapshot and descriptors
        return
    _PLAN_SNAPSHOT, _PLAN_DIRECTORIES = {}, {}
    try:
        yield
    finally:
        directories = _PLAN_DIRECTORIES
        _PLAN_SNAPSHOT = _PLAN_DIRECTORIES = None
        last = directories.get("last")
        if last is not None:
            os.close(last[1])  # type: ignore[index]
        if "root" in directories:
            os.close(directories["root"])  # type: ignore[arg-type]


def select_tests(files: list[str]) -> tuple[list[str], bool]:
    with _plan_snapshot():
        return _select_tests(files)


def _select_tests(files: list[str]) -> tuple[list[str], bool]:
    all_tests = discover_tests()
    classifier = load_classifier()
    executable_suffixes = set(classifier.EXECUTABLE_SUFFIXES)
    selected: set[str] = set()
    unknown_executable = False
    for raw in files:
        if "\\" in raw:
            # A literal backslash is a legal POSIX filename character; mapping
            # it to "/" could select a different file. Force full proof.
            unknown_executable = True
            continue
        path = raw.removeprefix("./")
        candidate = CANDIDATE_ROOT / path
        if (
            path.startswith("tests/")
            and candidate.suffix in TEST_SUFFIXES
            and candidate.name.startswith("test_")
            and (info := _candidate_stat(CANDIDATE_ROOT, path)) is not None
            and stat.S_ISREG(info.st_mode)
        ):
            selected.add(path)
            continue
        suffix = candidate.suffix.lower()
        if suffix not in executable_suffixes and candidate.name not in EXECUTABLE_NAMES:
            if not classifier.is_documentation_file(path):
                unknown_executable = True
            continue
        if suffix == ".py" and not path.startswith("tests/"):
            stem = candidate.stem.removeprefix("test_")
            impacted_modules, parse_failures = _impacted_closure(_module_name(path))
            if any(_module_name(source) not in impacted_modules for source in parse_failures):
                unknown_executable = True
            matches: list[str] = []
            for test in all_tests:
                if Path(test).stem == f"test_{stem}":
                    matches.append(test)
                    continue
                try:
                    if _reference_prefixes(test) & impacted_modules:
                        matches.append(test)
                except (OSError, SyntaxError, UnicodeError):
                    unknown_executable = True
            if matches:
                selected.update(matches)
                continue
        unknown_executable = True
    return sorted(selected), unknown_executable


def slice_matrix(files: list[str], count: int = 6) -> dict[str, list[dict[str, object]]]:
    durations_path = Path(os.environ.get("RUNTIME_OS_DURATIONS_PATH", CANDIDATE_ROOT / "test_durations.json"))
    durations = json.loads(durations_path.read_text(encoding="utf-8")) if durations_path.exists() else {}
    bucket_count = min(count, len(files))
    buckets: list[list[str]] = [[] for _ in range(bucket_count)]
    totals = [0.0] * bucket_count

    def duration(path: str) -> float:
        try:
            return max(0.0, float(durations.get(path, 1.0)))
        except (TypeError, ValueError):
            return 1.0

    weighted = sorted(files, key=lambda path: (-duration(path), path))
    for path in weighted:
        index = min(range(bucket_count), key=lambda item: (totals[item], item))
        buckets[index].append(path)
        totals[index] += duration(path)
    return {"include": [{"index": index + 1, "files": ":".join(bucket)} for index, bucket in enumerate(buckets) if bucket]}


def write_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def build_review_classification(classification: Any) -> dict[str, object]:
    review_classification = classification.as_dict()
    if classification.risk_class in {"R0", "R1", "R2"}:
        review_classification.update(
            required_reviews=[],
            secondary_review_required=False,
            adversarial_review_required=False,
            opposite_provider_required=False,
            opposite_frontier_required=False,
            human_gate_required=False,
            founder_review_required=False,
            model_tier_required=0,
        )
    elif classification.risk_class == "R3":
        review_classification.update(
            required_reviews=["adversarial_review_required"],
            secondary_review_required=False,
            adversarial_review_required=True,
            opposite_provider_required=False,
            opposite_frontier_required=False,
            human_gate_required=False,
            founder_review_required=False,
            model_tier_required=0,
        )
    return review_classification


def plan(args: argparse.Namespace) -> int:
    policy = load_policy()
    files = json.loads(args.changed_files_json)
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise ValueError("changed files must be a JSON string array")
    body = Path(args.body_file).read_text(encoding="utf-8") if args.body_file else ""
    classification = load_classifier().classify(files, body, additions=args.additions, pr_number=args.pr_number, repo=args.repo)
    review_classification = build_review_classification(classification)
    run_full, reason = full_proof(files, args.event_name, policy)
    selected, unknown = select_tests(files)
    if unknown:
        run_full, reason = True, "unknown_executable_fails_closed"
    tests = discover_tests() if run_full else selected
    # The matrix travels colon-joined; a path containing ':' would split into
    # decoy paths and the real file would never run.
    ambiguous = sorted(
        path for path in tests if ":" in path or any(ord(char) < 32 for char in path)
    )
    if ambiguous:
        raise ValueError(f"test paths cannot contain ':' or control characters: {ambiguous}")
    # Full proof means every unit slice plus e2e; an empty unit set would let
    # e2e alone satisfy it.
    if run_full and not tests:
        raise ValueError("full proof selected zero unit test files")
    matrix = slice_matrix(tests)
    review_key = "R4-R5" if classification.risk_class in {"R4", "R5"} else "R3" if classification.risk_class == "R3" else "R0-R2"
    result = {
        "policy_version": policy["policy_version"],
        "policy_source_commit": policy["source_commit"],
        "risk_class": classification.risk_class,
        "complexity_class": classification.complexity_class,
        "review_route": policy["review_model"][review_key],
        "full_proof": run_full,
        "reason": reason,
        "run_e2e": run_full,
        "matrix": matrix,
        "selected_test_count": len(tests),
        "telemetry_write_allowed": args.event_name == "push" and args.ref == "refs/heads/main",
    }
    encoded = json.dumps(result, separators=(",", ":"), sort_keys=True)
    print(json.dumps(result, indent=2, sort_keys=True))
    write_output("plan", encoded)
    write_output("matrix", json.dumps(matrix, separators=(",", ":")))
    write_output("risk_class", classification.risk_class)
    write_output("review_route", result["review_route"])
    write_output("review_classification", json.dumps(review_classification, separators=(",", ":"), sort_keys=True))
    write_output("run_e2e", str(run_full).lower())
    write_output("has_tests", str(bool(matrix["include"])).lower())
    write_output("telemetry_write_allowed", str(result["telemetry_write_allowed"]).lower())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--changed-files-json", required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--ref", default="")
    parser.add_argument("--body-file")
    parser.add_argument("--additions", type=int, default=0)
    parser.add_argument("--pr-number", default="unknown")
    parser.add_argument("--repo", default="unknown")
    return plan(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
