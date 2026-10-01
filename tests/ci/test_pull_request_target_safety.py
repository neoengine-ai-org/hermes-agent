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
# Shell commands that materialize, apply, or emit ref content (worktree or blob).
MATERIALIZING_GIT = re.compile(
    r"\bgit\b[^\n]*\b(checkout|switch|worktree|restore|reset|archive|read-tree|"
    r"checkout-index|stash|show|apply|am|cherry-pick|merge|rebase|pull)\b"
    r"|\bgit\b[^\n]*\bcat-file\s+(-p|blob)\b"
    r"|\bFETCH_HEAD\b"
    r"|\bgh\s+pr\s+(checkout|diff)\b",
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


def _job_reads_pr_refs(workflow: dict[Any, Any], job: dict[Any, Any]) -> bool:
    # Taint is job-wide: one step can fetch the head and a later step can
    # check out FETCH_HEAD or a ref recorded on disk without naming it again.
    scopes: list[Any] = [workflow.get("env"), job.get("env")]
    for step in job.get("steps") or []:
        if isinstance(step, dict):
            scopes.extend([step.get("env"), step.get("run"), step.get("with")])
    return any(FORBIDDEN_REF.search(str(scope)) for scope in scopes if scope)


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
        reads_pr_refs = _job_reads_pr_refs(workflow, job)
        for index, step in enumerate(job.get("steps") or []):
            if not isinstance(step, dict):
                continue
            step_where = f"{where}:step[{index}] {step.get('name', step.get('uses', ''))}".rstrip()
            # Any action input (checkout, alternate checkout actions, local
            # composite actions, inline scripts) naming PR-head content.
            for field, value in (step.get("with") or {}).items():
                if FORBIDDEN_REF.search(str(value)):
                    violations.append(
                        f"{step_where} passes {field}={value!r} to {step.get('uses')}"
                    )
            script = str(step.get("run") or "")
            if script and reads_pr_refs and MATERIALIZING_GIT.search(script):
                violations.append(
                    f"{step_where} materializes PR-head content in a job that reads PR refs"
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
        _prt([
            {"run": "git fetch origin ${{ github.event.pull_request.head.sha }}"},
            {"run": "git checkout FETCH_HEAD && make test"},
        ]),
        _prt([
            {
                "env": {"H": "${{ github.event.pull_request.head.sha }}"},
                "run": 'git fetch origin "$H"',
            },
            {"run": 'git show "$(cat head.txt)":setup.py | python -'},
        ]),
        _prt([
            {
                "uses": "some-org/checkout-pr@v1",
                "with": {"sha": "${{ github.event.pull_request.head.sha }}"},
            }
        ]),
        _prt([{"uses": "./.github/actions/run", "with": {"ref": "refs/pull/1/head"}}]),
        _prt([
            {
                "uses": "actions/github-script@v7",
                "with": {"script": "run('${{ github.event.pull_request.head.sha }}')"},
            }
        ]),
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
        _prt([
            {
                "env": {"HEAD_SHA": "${{ github.event.pull_request.head.sha }}"},
                "run": 'git fetch --no-tags origin "$HEAD_SHA"\ngit cat-file -e "${HEAD_SHA}^{commit}"',
            },
            {"run": "git -C trusted ls-files > files.txt\ngit diff --name-only a b"},
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
