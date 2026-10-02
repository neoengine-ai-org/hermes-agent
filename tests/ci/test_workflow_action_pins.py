"""Supply-chain pin checks for GitHub Actions references.

CONTRIBUTING.md requires every GitHub Action to be pinned to a full commit
SHA with a version comment (``uses: owner/action@<sha>  # vX.Y.Z``). A mutable
tag in a ``pull_request_target`` workflow runs with base-repo secrets and write
tokens on every qualifying PR event, so a retargeted tag or compromised release
becomes arbitrary code in a privileged context.

References are extracted from the composed YAML node tree, so flow-style
mappings, quoted keys and anchors are seen exactly as GitHub sees them, and
text inside ``run:`` scripts is never mistaken for a reference. Any ``uses``
key in a position this module does not recognise fails the test (fail closed).

These checks prove pin *shape* only. They do not prove that a SHA belongs to
the tag named in its comment; that is a review obligation.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github/workflows"


def _tracked_action_manifests() -> list[Path]:
    # Enumerate tracked files rather than pruning by directory name: a local
    # `uses: ./node_modules/x` is just as executable as `./.github/actions/x`.
    # No fallback: if trackedness cannot be established, collection fails.
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--", "*action.yml", "*action.yaml"],
        check=True,
        capture_output=True,
    ).stdout.decode()
    return sorted(ROOT / rel for rel in out.split("\0") if Path(rel).name in {"action.yml", "action.yaml"})


WORKFLOWS = sorted([*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")])
COMPOSITE_ACTIONS = _tracked_action_manifests()

_SEGMENT = r"(?!\.{1,2}(?:/|@))[A-Za-z0-9_.-]+"
PINNED_REMOTE = re.compile(rf"^{_SEGMENT}/{_SEGMENT}(?:/{_SEGMENT})*@[0-9a-f]{{40}}$")
VERSION_COMMENT = re.compile(r"^\s*#\s*v?\d+(?:\.\d+){0,2}\s*$")
# Mappings whose keys are user-defined data, never step/job structure.
_DATA_KEYS = {"with", "env", "inputs", "outputs", "secrets", "services", "container", "defaults"}


class UsesRef(NamedTuple):
    path: Path
    line: int  # 1-based
    ref: str
    comment: str
    kind: str  # "step" (action) or "job" (reusable workflow call)

    def where(self) -> str:
        return f"{self.path.relative_to(ROOT)}:{self.line}: {self.ref}"


def _key(node: yaml.Node) -> str | None:
    return node.value if isinstance(node, yaml.ScalarNode) else None


def _collect_uses(path: Path, text: str | None = None) -> tuple[list[UsesRef], list[str]]:
    """Return (references, unrecognised-position errors) for one YAML file."""
    text = path.read_text(encoding="utf-8") if text is None else text
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    assert isinstance(root, yaml.MappingNode), f"{path} did not compose to a mapping"
    lines = text.splitlines()
    refs: list[UsesRef] = []
    errors: list[str] = []
    seen: set[int] = set()

    def record(value: yaml.Node, trail: tuple[str, ...], kind: str) -> None:
        if id(value) in seen:  # anchors/aliases share one node
            return
        seen.add(id(value))
        if not isinstance(value, yaml.ScalarNode):
            errors.append(f"{path.relative_to(ROOT)}:{value.start_mark.line + 1}: non-scalar uses at {'.'.join(trail)}")
            return
        line = lines[value.end_mark.line] if value.end_mark.line < len(lines) else ""
        # Drop flow-collection closers so `- {uses: x@sha}  # v1` keeps its comment.
        comment = re.sub(r"^[\s}\]]*", "", line[value.end_mark.column :])
        refs.append(UsesRef(path, value.start_mark.line + 1, value.value, comment, kind))

    def walk(node: yaml.Node, trail: tuple[str, ...]) -> None:
        if isinstance(node, yaml.MappingNode):
            for key_node, value in node.value:
                key = _key(key_node)
                if key == "uses":
                    if len(trail) == 2 and trail[0] == "jobs":  # reusable workflow call
                        record(value, trail, "job")
                    elif len(trail) >= 2 and trail[-1] == "[]" and trail[-2] == "steps":
                        record(value, trail, "step")
                    else:
                        errors.append(
                            f"{path.relative_to(ROOT)}:{key_node.start_mark.line + 1}: "
                            f"uses at unrecognised position {'.'.join(trail) or '<root>'}"
                        )
                elif (key in _DATA_KEYS or key == "run") and trail[-1:] != ("jobs",):
                    continue  # a job *named* env/run/with is still walked
                else:
                    walk(value, (*trail, str(key)))
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                walk(item, (*trail, "[]"))

    walk(root, ())
    return refs, errors


def _triggers(path: Path) -> set[str]:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict), f"{path} did not parse to a mapping"
    # PyYAML (YAML 1.1) parses a bare `on:` key as boolean True. Union both
    # spellings so a file declaring `on:` and `"on":` cannot hide a trigger.
    names: set[str] = set()
    for key in (True, "on"):
        value = workflow.get(key)
        if isinstance(value, str):
            names.add(value)
        elif isinstance(value, (list, dict)):
            names.update(str(item) for item in value)
    return names


def _offenders(refs: list[UsesRef], *, allow_local: bool) -> list[str]:
    bad = []
    for ref in refs:
        if allow_local and ref.ref.startswith("./"):
            continue
        if not PINNED_REMOTE.match(ref.ref) or not VERSION_COMMENT.match(ref.comment):
            bad.append(ref.where())
    return bad


PRT_WORKFLOWS = [path for path in WORKFLOWS if "pull_request_target" in _triggers(path)]


def test_discovery_is_not_empty() -> None:
    # An empty parametrize list is a pytest skip, not a failure.
    assert WORKFLOWS, "no workflows discovered"
    assert COMPOSITE_ACTIONS, "no composite actions discovered"
    # The privileged pull_request_target canary; #99 replaced the old auto-arm
    # workflow with this disarm-only reconciler.
    assert "auto-merge-disarm-reconciler.yml" in {path.name for path in PRT_WORKFLOWS}
    assert sum(len(_collect_uses(path)[0]) for path in WORKFLOWS) > 0


@pytest.mark.parametrize("path", PRT_WORKFLOWS, ids=lambda p: p.name)
def test_pull_request_target_uses_are_sha_pinned(path: Path) -> None:
    refs, errors = _collect_uses(path)
    offenders = errors + _offenders(refs, allow_local=False)
    assert not offenders, (
        "pull_request_target workflows must pin every action to a full 40-hex "
        "commit SHA with a version comment (no tags, branches, ./local or "
        "docker:// refs):\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize("path", [*WORKFLOWS, *COMPOSITE_ACTIONS], ids=lambda p: str(p.relative_to(ROOT)))
def test_every_remote_action_is_sha_pinned(path: Path) -> None:
    refs, errors = _collect_uses(path)
    offenders = errors + _offenders(refs, allow_local=True)
    assert not offenders, (
        "remote actions must be pinned to a 40-hex SHA with a version comment:\n" + "\n".join(offenders)
    )


def _local_target_errors(refs: list[UsesRef]) -> list[str]:
    # Normalise first, then check by structural kind: a step must land on a
    # scanned tracked manifest directory, a job call on a scanned workflow file.
    scanned_actions = {path.parent.resolve() for path in COMPOSITE_ACTIONS}
    scanned_workflows = {path.resolve() for path in WORKFLOWS}
    errors = []
    for ref in refs:
        if not ref.ref.startswith("./"):
            continue  # remote refs are pattern-checked
        target = (ROOT / ref.ref).resolve()
        allowed = scanned_workflows if ref.kind == "job" else scanned_actions
        if target not in allowed:
            errors.append(f"{ref.where()} (local {ref.kind} target is not a scanned tracked file)")
    return errors


@pytest.mark.parametrize("path", [*WORKFLOWS, *COMPOSITE_ACTIONS], ids=lambda p: str(p.relative_to(ROOT)))
def test_local_actions_resolve_to_scanned_manifests(path: Path) -> None:
    # allow_local only defers trust to the target, so the target must be pin-checked too.
    errors = _local_target_errors(_collect_uses(path)[0])
    assert not errors, "\n".join(errors)


def test_local_target_outside_scan_is_rejected() -> None:
    refs, _ = _collect_uses(
        ROOT / "synthetic.yml",
        "jobs:\n"
        "  j:\n"
        "    steps:\n"
        "      - uses: ./node_modules/bridge\n"
        "      - uses: ./.github/workflows/../../node_modules/bridge\n"
        "      - uses: ./.github/workflows/tests.yml\n"  # a workflow is not a step action
        "      - uses: ./.github/actions/nix-setup\n"
        "      - uses: ./.github/actions/nix-setup/\n"
        "  k:\n"
        "    uses: ./.github/workflows/../../node_modules/bridge\n"
        "  ok:\n"
        "    uses: ./.github/workflows/tests.yml\n",
    )
    errors = _local_target_errors(refs)
    assert len(errors) == 4, errors
    assert not any("nix-setup" in e or ":12:" in e for e in errors)


@pytest.mark.parametrize(
    "ref",
    [
        "actions/create-github-app-token@v2",
        "actions/checkout@main",
        "actions/checkout@de0fac2e",  # short SHA
        "actions/checkout@DE0FAC2E4500DABE0009E67214FF5F5447CE83DD",  # non-canonical uppercase hex
        "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd0",  # 41 hex
        "./.github/actions/nix-setup",
        "docker://alpine:3",
        "../../evil@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
        "a/b/../../c@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    ],
)
def test_pin_pattern_rejects_mutable_or_malformed_refs(ref: str) -> None:
    assert not PINNED_REMOTE.match(ref)


def test_pin_pattern_accepts_sha_pins() -> None:
    assert PINNED_REMOTE.match("actions/create-github-app-token@fee1f7d63c2ff003460e3d139729b119787bc349")
    assert PINNED_REMOTE.match("actions/cache/restore@27d5ce7f107fe9357f9df03efb73ab90386fccae")
    assert PINNED_REMOTE.match("org/repo/.github/workflows/w.yml@27d5ce7f107fe9357f9df03efb73ab90386fccae")


@pytest.mark.parametrize("comment", ["  # v2.2.2", " # 8.2.0", " # v7", "#v22"])
def test_version_comment_accepts_versions(comment: str) -> None:
    assert VERSION_COMMENT.match(comment)


@pytest.mark.parametrize("comment", ["", " # 2 apples", " # latest", " v2.2.2"])
def test_version_comment_rejects_non_versions(comment: str) -> None:
    assert not VERSION_COMMENT.match(comment)


SHA = "de0fac2e4500dabe0009e67214ff5f5447ce83dd"


@pytest.mark.parametrize(
    "body",
    [
        "jobs:\n  j:\n    steps:\n      - {uses: actions/checkout@v4}\n",
        'jobs:\n  j:\n    steps:\n      - "uses": actions/checkout@v4\n',
        'jobs: {j: {steps: [{"uses": "actions/checkout@v4"}]}}\n',
        "jobs:\n  j:\n    uses: org/repo/.github/workflows/w.yml@main\n",
        "runs:\n  steps:\n    - uses: actions/checkout@v4\n",
        f"jobs:\n  j:\n    steps:\n      - uses: actions/checkout@{SHA}\n",  # no version comment
        "jobs:\n  j:\n    stepz:\n      - uses: actions/checkout@v4\n",  # unrecognised position
        "jobs:\n  env:\n    steps:\n      - uses: actions/checkout@v4\n",  # job named like a data key
    ],
)
def test_collector_flags_bypass_shapes(body: str) -> None:
    refs, errors = _collect_uses(ROOT / "synthetic.yml", body)
    assert errors or _offenders(refs, allow_local=True)


def test_collector_ignores_run_bodies_and_dedupes_anchors() -> None:
    body = (
        "jobs:\n"
        "  j:\n"
        "    steps:\n"
        f"      - &co {{uses: actions/checkout@{SHA}}}  # v6.0.2\n"
        "      - *co\n"
        "      - run: |\n"
        "          cat <<'EOF'\n"
        "          uses: actions/checkout@v4\n"
        "          EOF\n"
        "      - uses: actions/github-script@" + SHA + "\n"
        "        with:\n"
        "          uses: not-an-action\n"
        "        # trailing note\n"
    )
    refs, errors = _collect_uses(ROOT / "synthetic.yml", body)
    assert not errors
    assert [ref.ref for ref in refs] == [f"actions/checkout@{SHA}", f"actions/github-script@{SHA}"]
    # Flow-style comment after the closing brace is still the version comment.
    assert _offenders(refs[:1], allow_local=False) == []
    assert len(_offenders(refs, allow_local=False)) == 1  # github-script lacks a version comment
