#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import contextlib
import errno
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
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


# Discovery and reads use the immutable git objects of the candidate commit --
# the commit checked out at ``CANDIDATE_ROOT``, pinned to its full SHA once
# per plan -- never the live filesystem. The test universe is exactly what the
# execution checkout of that commit contains: untracked files and bytecode
# caches do not exist in it, contents are content-addressed blobs, and a
# concurrent change to the working tree cannot alter a plan.
_REGULAR_MODES = {"100644", "100755"}
_SYMLINK_MODE = "120000"
_GITLINK_MODE = "160000"
_GIT_ENVIRONMENT = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
}

# (root, commit) -> {path: (mode, object id)}; immutable for a given commit.
_TREE_ENTRIES: dict[tuple[Path, str], dict[str, tuple[str, str]]] = {}
# blob id -> raw bytes awaiting parse, and blob id -> parsed references /
# dotted prefixes; content-addressed, so never stale.
_PENDING_BLOBS: dict[str, bytes] = {}
_REFERENCES_BY_BLOB: dict[str, frozenset[str]] = {}
_PREFIXES_BY_BLOB: dict[str, frozenset[str]] = {}
_PLAN: dict[str, object] | None = None


def _git(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    """Run plumbing git in ``root`` with a sanitized environment."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(_GIT_ENVIRONMENT)
    completed = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(root), *args],
        input=stdin,
        capture_output=True,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"git {args[0]} failed in {root}: {detail}")
    return completed.stdout


def _resolve_candidate_commit(root: Path) -> str:
    """Full SHA of the commit checked out at ``root`` (the repository top level)."""
    if os.path.realpath(root) != str(root):
        raise ValueError(f"candidate root must not be a symlink or contain one: {root}")
    top_level = _git(root, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    if os.path.realpath(top_level) != str(root):
        raise ValueError(f"candidate root must be the repository top level: {root} (found {top_level})")
    commit = _git(root, "rev-parse", "--verify", "--end-of-options", "HEAD^{commit}").decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ValueError(f"unexpected commit id from git: {commit!r}")
    return commit


def _tree_entries(root: Path, commit: str) -> dict[str, tuple[str, str]]:
    key = (root, commit)
    entries = _TREE_ENTRIES.get(key)
    if entries is None:
        entries = {}
        for record in _git(root, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
            if not record:
                continue
            header, _, raw_path = record.partition(b"\t")
            mode, kind, object_id = header.decode("ascii").split(" ")
            path = raw_path.decode("utf-8", "surrogateescape")
            if kind not in {"blob", "commit"}:
                raise ValueError(f"unexpected tree entry {kind} for {path!r}")
            entries[path] = (mode, object_id)
        _TREE_ENTRIES[key] = entries
    return entries


def _candidate_tree() -> tuple[str, dict[str, tuple[str, str]]]:
    """The pinned candidate commit and its entries (one commit per plan)."""
    if _PLAN is not None and _PLAN.get("root") == CANDIDATE_ROOT and _PLAN.get("commit"):
        commit = str(_PLAN["commit"])
    else:
        commit = _resolve_candidate_commit(CANDIDATE_ROOT)
        if _PLAN is not None:
            _PLAN["root"], _PLAN["commit"] = CANDIDATE_ROOT, commit
    return commit, _tree_entries(CANDIDATE_ROOT, commit)


def _tree_python_files(prefix: tuple[str, ...], prune, refuse_links_when_pruned: bool) -> list[str]:
    """Regular ``*.py`` blobs of the pinned commit under ``prefix``.

    Python files below a directory for which ``prune(parts)`` is true are
    skipped, but pruning never hides a link: a gitlink anywhere under
    ``prefix`` raises (the execution checkout does not materialize
    submodules, so its tests would silently be missing), and so does any
    symlink outside pruned directories -- or anywhere, when
    ``refuse_links_when_pruned`` (the test tree, whose pruned e2e,
    integration and docker suites are collected by their own jobs) -- since
    pytest could collect through it and a link can resolve differently in
    another checkout. A ``prefix`` absent from, or not a directory in, the
    commit raises too.
    """
    _, entries = _candidate_tree()
    label = "/".join(prefix)
    if prefix and label in entries:
        mode = entries[label][0]
        if mode == _SYMLINK_MODE:
            raise ValueError(f"runtime-OS discovery refuses symlink: {label}")
        raise NotADirectoryError(errno.ENOTDIR, "Not a directory in the candidate commit", label)
    found: list[str] = []
    seen_prefix = not prefix
    for path, (mode, _) in entries.items():
        parts = tuple(path.split("/"))
        if parts[: len(prefix)] != prefix:
            continue
        seen_prefix = True
        if mode == _GITLINK_MODE:
            raise ValueError(f"runtime-OS discovery refuses submodule: {path}")
        pruned = _pruned(parts, len(prefix), prune)
        if mode == _SYMLINK_MODE and (refuse_links_when_pruned or not pruned):
            raise ValueError(f"runtime-OS discovery refuses symlink: {path}")
        if not pruned and mode in _REGULAR_MODES and path.endswith(".py"):
            found.append(path)
    if not seen_prefix:
        raise FileNotFoundError(errno.ENOENT, "Not present in the candidate commit", label)
    return found


_TEST_SKIP_PARTS = {"integration", "e2e", "docker"}
_SOURCE_EXCLUDED = {".git", ".venv", "tests", "venv"}
# Generated interpreter/environment trees at the repository root are not
# source: the bootstrap proof venv and the restored CI environment (which
# carries a whole CPython stdlib under ci-fast/) would otherwise be parsed.
_SOURCE_GENERATED_ROOTS = {".bootstrap-proof-venv", "ci-fast"}


def _pruned(parts: tuple[str, ...], start: int, prune) -> bool:
    """Whether a directory of ``parts`` below its first ``start`` parts is pruned."""
    return any(prune(parts[: index + 1]) for index in range(start, len(parts) - 1))


def _prune_tests(parts: tuple[str, ...]) -> bool:
    return parts[-1] in _TEST_SKIP_PARTS


def _prune_sources(parts: tuple[str, ...]) -> bool:
    return parts[-1] in _SOURCE_EXCLUDED or (len(parts) == 1 and parts[0] in _SOURCE_GENERATED_ROOTS)


def _is_test_file(path: str) -> bool:
    return Path(path).name.startswith("test_") and not (set(Path(path).parts) & _TEST_SKIP_PARTS)


def _is_source_file(path: str) -> bool:
    return not (set(Path(path).parts) & _SOURCE_EXCLUDED)


def discover_tests() -> list[str]:
    return sorted(
        path
        for path in _tree_python_files(("tests",), _prune_tests, refuse_links_when_pruned=True)
        if _is_test_file(path)
    )


def discover_python_sources() -> list[str]:
    return sorted(
        path
        for path in _tree_python_files((), _prune_sources, refuse_links_when_pruned=False)
        if _is_source_file(path)
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


def _decode_source(data: bytes) -> str:
    """Decode exactly like ``Path.read_text(encoding="utf-8")`` (universal newlines)."""
    return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8").read()


def _in_discoverable_universe(path: str) -> bool:
    """Whether ``discover_tests()`` or ``discover_python_sources()`` could return
    the regular ``*.py`` blob at ``path`` (same prune and filter predicates)."""
    parts = tuple(path.split("/"))
    if parts[0] == "tests" and not _pruned(parts, 1, _prune_tests) and _is_test_file(path):
        return True
    return not _pruned(parts, 0, _prune_sources) and _is_source_file(path)


def _fetch_blobs(object_ids: list[str]) -> None:
    """Load blobs with one ``git cat-file --batch``, verifying id and type."""
    if not object_ids:
        return
    output = _git(CANDIDATE_ROOT, "cat-file", "--batch", stdin="".join(f"{oid}\n" for oid in object_ids).encode("ascii"))
    offset = 0
    for object_id in object_ids:
        newline = output.index(b"\n", offset)
        header = output[offset:newline].decode("ascii").split(" ")
        if len(header) != 3 or header[0] != object_id or header[1] != "blob":
            raise ValueError(f"git returned {header!r} for blob {object_id}")
        size = int(header[2])
        start = newline + 1
        _PENDING_BLOBS[object_id] = output[start : start + size]
        if output[start + size : start + size + 1] != b"\n":
            raise ValueError(f"malformed git cat-file output for blob {object_id}")
        offset = start + size + 1


def _blob_for(path: str) -> str:
    """Blob id of ``path`` in the pinned commit; a symlink or non-file raises."""
    _, entries = _candidate_tree()
    entry = entries.get(path)
    if entry is None:
        raise FileNotFoundError(errno.ENOENT, "Not present in the candidate commit", path)
    mode, object_id = entry
    if mode == _SYMLINK_MODE:
        raise ValueError(f"runtime-OS discovery refuses symlink: {path}")
    if mode not in _REGULAR_MODES:
        raise ValueError(f"{path} is not a regular file in the candidate commit")
    if object_id not in _REFERENCES_BY_BLOB and object_id not in _PENDING_BLOBS:
        # Fetch every unparsed blob discovery can return at once (pruned
        # environments such as .venv, and non-test helpers under tests/ such
        # as conftest.py, are fetched only when read directly).
        _fetch_blobs(
            sorted(
                {
                    oid
                    for name, (entry_mode, oid) in entries.items()
                    if entry_mode in _REGULAR_MODES
                    and name.endswith(".py")
                    and _in_discoverable_universe(name)
                    and oid not in _REFERENCES_BY_BLOB
                    and oid not in _PENDING_BLOBS
                }
                | {object_id}
            )
        )
    return object_id


def _candidate_source(path: str) -> str:
    object_id = _blob_for(path)
    data = _PENDING_BLOBS.get(object_id)
    if data is None:
        _fetch_blobs([object_id])
        data = _PENDING_BLOBS[object_id]
    return _decode_source(data)


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
    object_id = _blob_for(path)
    references = _REFERENCES_BY_BLOB.get(object_id)
    if references is None:
        references = _parse_references(_candidate_source(path), path)
        _REFERENCES_BY_BLOB[object_id] = references
        _PENDING_BLOBS.pop(object_id, None)
    return references


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
    object_id = _blob_for(path)
    prefixes = _PREFIXES_BY_BLOB.get(object_id)
    if prefixes is not None:
        return prefixes
    references = _module_references(path)
    if prefixes is None:
        expanded: set[str] = set()
        for reference in references:
            parts = reference.split(".")
            expanded.update(".".join(parts[: index + 1]) for index in range(len(parts)))
        prefixes = _PREFIXES_BY_BLOB[object_id] = frozenset(expanded)
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
    """Pin one candidate commit for the whole plan (nested plans share it)."""
    global _PLAN
    if _PLAN is not None:
        yield
        return
    _PLAN = {}
    try:
        yield
    finally:
        _PLAN = None


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
        if path.startswith("tests/") and candidate.suffix in TEST_SUFFIXES and candidate.name.startswith("test_"):
            mode = _candidate_tree()[1].get(path, ("",))[0]
            if mode == _SYMLINK_MODE:
                raise ValueError(f"runtime-OS discovery refuses symlink: {path}")
            if mode in _REGULAR_MODES:
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
    with _plan_snapshot():  # selection and full-proof discovery see one commit
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
