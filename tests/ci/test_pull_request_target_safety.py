"""Regression guard: privileged pull_request_target workflows never materialize PR-head code.

A ``pull_request_target`` job runs with base-repository privileges (secrets,
base-scoped cache writes, a token that may be write-capable). Checking out
``github.event.pull_request.head.*`` or ``refs/pull/*`` there lets a pull
request alter what that privileged job executes. PR content may only be read
as data (event fields, API responses, or git objects fetched without a
worktree); execution belongs in an unprivileged ``pull_request`` workflow.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"

# Expressions or refs that name pull-request head content.
FORBIDDEN_REF = re.compile(
    r"github\.event\.pull_request\.head\.|github\.head_ref|"
    r"github\.event\.pull_request\.merge_commit_sha|refs/pull/|\bpull/[^/\s]+/(?:head|merge)\b",
)
# Shell commands that materialize a ref as a worktree.
MATERIALIZING_GIT = re.compile(
    r"\bgit\b[^\n]*\b(checkout|switch|worktree|restore|reset|archive|read-tree|checkout-index|stash)\b"
    r"|\bgh\s+pr\s+checkout\b",
)


def _triggers(workflow: dict[Any, Any]) -> set[str]:
    # PyYAML (YAML 1.1) parses a bare ``on`` key as boolean True; GitHub reads
    # both spellings, so a workflow carrying both must be judged on their union.
    names: set[str] = set()
    for key in ("on", True):
        value = workflow.get(key)
        if isinstance(value, str):
            names.add(value)
        elif isinstance(value, list):
            names.update(str(item) for item in value)
        elif isinstance(value, dict):
            names.update(str(item) for item in value)
    return names


def _env_names_bound_to_pr_refs(*scopes: Any) -> set[str]:
    tainted: set[str] = set()
    for scope in scopes:
        if isinstance(scope, dict):
            for name, value in (scope.get("env") or {}).items():
                if FORBIDDEN_REF.search(str(value)):
                    tainted.add(str(name))
    return tainted


def pull_request_target_violations(workflow: dict[Any, Any], label: str) -> list[str]:
    """Return every place a pull_request_target workflow materializes PR-head content."""
    if "pull_request_target" not in _triggers(workflow):
        return []
    violations: list[str] = []
    for job_id, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        where = f"{label}:{job_id}"
        if job.get("uses") and FORBIDDEN_REF.search(str(job.get("with") or {})):
            violations.append(
                f"{where} passes a PR-head ref to reusable workflow {job['uses']}"
            )
        for index, step in enumerate(job.get("steps") or []):
            if not isinstance(step, dict):
                continue
            step_where = f"{where}:step[{index}] {step.get('name', step.get('uses', ''))}".rstrip()
            uses = str(step.get("uses") or "")
            with_ = step.get("with") or {}
            if uses.startswith("actions/checkout"):
                for field in ("ref", "repository"):
                    if FORBIDDEN_REF.search(str(with_.get(field, ""))):
                        violations.append(
                            f"{step_where} checks out {field}={with_[field]!r}"
                        )
            script = str(step.get("run") or "")
            if script and MATERIALIZING_GIT.search(script):
                tainted = _env_names_bound_to_pr_refs(workflow, job, step)
                references_tainted = any(
                    re.search(rf"\$(?:\{{)?{re.escape(name)}\b", script)
                    for name in tainted
                )
                if FORBIDDEN_REF.search(script) or references_tainted:
                    violations.append(
                        f"{step_where} materializes a PR-head ref in a run script"
                    )
    return violations


def _load(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def test_no_pull_request_target_workflow_checks_out_pull_request_head() -> None:
    scanned = []
    violations: list[str] = []
    for path in _workflow_files():
        workflow = _load(path)
        if "pull_request_target" in _triggers(workflow):
            scanned.append(path.name)
        violations.extend(pull_request_target_violations(workflow, path.name))
    # Not vacuous: the privileged Runtime OS advisory must still be scanned.
    assert "ci-runtime-os-advisory.yml" in scanned
    assert violations == []


def _prt(steps: list[dict[str, Any]], **job: Any) -> dict[Any, Any]:
    return {True: {"pull_request_target": {}}, "jobs": {"j": {"steps": steps, **job}}}


@pytest.mark.parametrize(
    "workflow",
    [
        _prt([
            {
                "uses": "actions/checkout@v6",
                "with": {"ref": "${{ github.event.pull_request.head.sha }}"},
            }
        ]),
        _prt([
            {
                "uses": "actions/checkout@v6",
                "with": {"ref": "${{ github.event.pull_request.head.ref }}"},
            }
        ]),
        _prt([
            {
                "uses": "actions/checkout@v6",
                "with": {"ref": "refs/pull/${{ github.event.number }}/merge"},
            }
        ]),
        _prt([
            {"uses": "actions/checkout@v6", "with": {"ref": "${{ github.head_ref }}"}}
        ]),
        _prt([
            {
                "uses": "actions/checkout@v6",
                "with": {
                    "repository": "${{ github.event.pull_request.head.repo.full_name }}"
                },
            }
        ]),
        _prt([
            {
                "uses": "actions/checkout@v6",
                "with": {
                    "ref": "${{ github.event.pull_request.head.sha || github.sha }}"
                },
            }
        ]),
        _prt([{"run": "git checkout ${{ github.event.pull_request.head.sha }}"}]),
        _prt([
            {
                "run": "git fetch origin pull/1/head && git checkout FETCH_HEAD refs/pull/1/head"
            }
        ]),
        _prt([{"run": "git fetch origin pull/7/head && git checkout FETCH_HEAD"}]),
        _prt([
            {
                "env": {"HEAD": "${{ github.event.pull_request.head.sha }}"},
                "run": 'git worktree add pr "$HEAD"',
            }
        ]),
        _prt(
            [{"run": "git archive ${HEAD_SHA} | tar -x"}],
            env={"HEAD_SHA": "${{ github.event.pull_request.head.sha }}"},
        ),
        _prt([{"run": "gh pr checkout ${{ github.event.pull_request.head.ref }}"}]),
        {
            "on": ["pull_request_target"],
            "jobs": {
                "j": {
                    "uses": "./.github/workflows/x.yml",
                    "with": {"ref": "${{ github.event.pull_request.head.sha }}"},
                }
            },
        },
        {
            "on": "push",
            True: ["pull_request_target"],
            "jobs": {
                "j": {
                    "steps": [
                        {
                            "uses": "actions/checkout@v6",
                            "with": {"ref": "refs/pull/1/head"},
                        }
                    ]
                }
            },
        },
    ],
)
def test_detector_flags_pull_request_head_materialization(
    workflow: dict[Any, Any],
) -> None:
    assert pull_request_target_violations(workflow, "synthetic")


@pytest.mark.parametrize(
    "workflow",
    [
        # Base-ref checkout and inert object fetch + diff are allowed.
        _prt([
            {
                "uses": "actions/checkout@v6",
                "with": {"ref": "${{ github.event.pull_request.base.sha }}"},
            }
        ]),
        _prt([{"uses": "actions/checkout@v6"}]),
        _prt([
            {
                "env": {"HEAD_SHA": "${{ github.event.pull_request.head.sha }}"},
                "run": 'git fetch --no-tags origin "$HEAD_SHA"\ngit diff --name-only "$BASE" "$HEAD_SHA"',
            }
        ]),
        # PR fields as data for API calls.
        _prt([
            {
                "uses": "actions/github-script@v7",
                "env": {"EXPECTED_HEAD": "${{ github.event.pull_request.head.sha }}"},
            }
        ]),
        # Head checkout is fine in an unprivileged pull_request workflow.
        {
            True: {"pull_request": {}},
            "jobs": {
                "j": {
                    "steps": [
                        {
                            "uses": "actions/checkout@v6",
                            "with": {
                                "ref": "${{ github.event.pull_request.head.sha }}"
                            },
                        }
                    ]
                }
            },
        },
    ],
)
def test_detector_allows_base_code_and_pr_data(workflow: dict[Any, Any]) -> None:
    assert pull_request_target_violations(workflow, "synthetic") == []
