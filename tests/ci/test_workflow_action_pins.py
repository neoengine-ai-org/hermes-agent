"""Supply-chain pin checks for GitHub Actions references.

CONTRIBUTING.md requires every GitHub Action to be pinned to a full commit
SHA with a version comment. A mutable tag in a ``pull_request_target``
workflow runs with base-repo secrets and write tokens on every qualifying PR
event, so a retargeted tag or compromised release becomes arbitrary code in a
privileged context. These tests fail closed: any ``uses:`` reference that is
not provably ``owner/repo[/path]@<40-hex>`` is rejected in
``pull_request_target`` workflows, and every remote reference anywhere must be
SHA-pinned.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github/workflows"
ACTIONS_DIR = ROOT / ".github/actions"

WORKFLOWS = sorted([*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")])
COMPOSITE_ACTIONS = sorted([*ACTIONS_DIR.glob("*/action.yml"), *ACTIONS_DIR.glob("*/action.yaml")])

PINNED_REMOTE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[^@\s]+)?@[0-9a-f]{40}$")
# Any line whose YAML key is `uses`, whether or not it starts a sequence item.
RAW_USES_LINE = re.compile(r"^\s*(?:-\s+)?uses\s*:\s*(\S+)(.*)$")
VERSION_COMMENT = re.compile(r"#\s*v?\d+(?:\.\d+)*\b")


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path} did not parse to a mapping"
    return data


def _triggers(workflow: dict) -> set[str]:
    # PyYAML (YAML 1.1) parses a bare `on:` key as boolean True. Union both
    # spellings so a file declaring `on:` and `"on":` cannot hide a trigger.
    names: set[str] = set()
    for key in (True, "on"):
        value = workflow.get(key)
        if isinstance(value, str):
            names.add(value)
        elif isinstance(value, list):
            names.update(str(item) for item in value)
        elif isinstance(value, dict):
            names.update(str(item) for item in value)
    return names


def _parsed_uses(document: dict) -> list[str]:
    refs: list[str] = []
    for job in (document.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        if "uses" in job:  # reusable workflow call
            refs.append(str(job["uses"]))
        for step in job.get("steps") or []:
            if isinstance(step, dict) and "uses" in step:
                refs.append(str(step["uses"]))
    for step in (document.get("runs") or {}).get("steps") or []:  # composite action
        if isinstance(step, dict) and "uses" in step:
            refs.append(str(step["uses"]))
    return refs


def _raw_uses(path: Path) -> list[tuple[int, str, str]]:
    found = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = RAW_USES_LINE.match(line)
        if match:
            found.append((lineno, match.group(1).strip("'\""), match.group(2)))
    return found


PRT_WORKFLOWS = [path for path in WORKFLOWS if "pull_request_target" in _triggers(_load(path))]


def test_pull_request_target_workflows_are_discovered() -> None:
    names = {path.name for path in PRT_WORKFLOWS}
    assert "auto-arm-auto-merge.yml" in names


@pytest.mark.parametrize("path", PRT_WORKFLOWS, ids=lambda p: p.name)
def test_pull_request_target_uses_are_sha_pinned(path: Path) -> None:
    raw = _raw_uses(path)
    parsed = _parsed_uses(_load(path))
    # Fail closed if the structural walk missed a `uses:` the text contains.
    assert sorted(ref for _, ref, _ in raw) == sorted(parsed), (
        f"{path.name}: uses: references outside jobs.*.uses / jobs.*.steps[*].uses"
    )
    offenders = [
        f"{path.name}:{lineno}: {ref}"
        for lineno, ref, tail in raw
        if not PINNED_REMOTE.match(ref) or not VERSION_COMMENT.search(tail)
    ]
    assert not offenders, (
        "pull_request_target workflows must pin every action to a full 40-hex "
        "commit SHA with a version comment (no tags, branches, ./local or "
        "docker:// refs):\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize("path", [*WORKFLOWS, *COMPOSITE_ACTIONS], ids=lambda p: str(p.relative_to(ROOT)))
def test_every_remote_action_is_sha_pinned(path: Path) -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{lineno}: {ref}"
        for lineno, ref, _ in _raw_uses(path)
        if not ref.startswith("./") and not PINNED_REMOTE.match(ref)
    ]
    assert not offenders, "remote actions must be pinned to a 40-hex SHA:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "ref",
    [
        "actions/create-github-app-token@v2",
        "actions/checkout@main",
        "actions/checkout@de0fac2e",  # short SHA
        "actions/checkout@DE0FAC2E4500DABE0009E67214FF5F5447CE83DD",  # non-canonical uppercase hex
        "./.github/actions/nix-setup",
        "docker://alpine:3",
        "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd0",  # 41 hex
    ],
)
def test_pin_pattern_rejects_mutable_refs(ref: str) -> None:
    assert not PINNED_REMOTE.match(ref)


def test_pin_pattern_accepts_sha_pins() -> None:
    assert PINNED_REMOTE.match("actions/create-github-app-token@fee1f7d63c2ff003460e3d139729b119787bc349")
    assert PINNED_REMOTE.match("actions/cache/restore@27d5ce7f107fe9357f9df03efb73ab90386fccae")
