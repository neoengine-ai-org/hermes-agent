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
# Contexts a pull_request_target checkout may resolve its ref/repository from.
# Anything else (PR head fields, step outputs, env indirection, inputs) fails.
TRUSTED_CHECKOUT_CONTEXTS = (
    "github.sha",
    "github.ref",
    "github.base_ref",
    "github.repository",
    "github.event.pull_request.base.",
    "github.event.merge_group.",
    "github.event.repository.",
)
EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.S)
CONTEXT_PATH = re.compile(
    r"\b(?:github|env|steps|needs|inputs|vars|matrix|job|runner|strategy|secrets)\.[\w.*-]+"
)
GIT_CALL = re.compile(
    r"(?<![\w./-])git(?:\s+(?:-C|-c)\s+\S+|\s+--no-pager)*\s+([a-z][\w-]*)([^\n;&|)]*)"
)
GH_CHECKOUT = re.compile(r"(?<![\w./-])gh\s+pr\s+(?:checkout|diff)\b")
SHELL_COMMENT = re.compile(r"(?m)(?:^|(?<=\s))#.*$")
NETWORK_TOOL = re.compile(r"(?<![\w./-])(?:curl|wget|gh\s+api)\b")


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


def _tainted_env(*scopes: Any) -> set[str]:
    return {
        str(name)
        for scope in scopes
        if isinstance(scope, dict)
        for name, value in (scope.get("env") or {}).items()
        if FORBIDDEN_REF.search(str(value))
    }


def _names_pr_ref(value: Any, tainted: set[str]) -> bool:
    """An action input names PR-head content directly or via ``${{ env.X }}``."""
    text = str(value)
    if FORBIDDEN_REF.search(text):
        return True
    return any(
        re.search(rf"\benv\.{re.escape(name)}\b", expression)
        for expression in EXPRESSION.findall(text)
        for name in tainted
    )


def _shell_names_pr_ref(line: str, tainted: set[str]) -> bool:
    if FORBIDDEN_REF.search(line):
        return True
    return any(re.search(rf"\$\{{?{re.escape(name)}\b", line) for name in tainted)


def _context_matches(path: str, prefix: str) -> bool:
    if prefix.endswith("."):
        return path.startswith(prefix)
    return path == prefix or path.startswith(prefix + "_")


def _untrusted_checkout_value(value: Any) -> bool:
    text = str(value)
    literal = EXPRESSION.sub("", text)
    if FORBIDDEN_REF.search(literal):
        return True
    for expression in EXPRESSION.findall(text):
        for path in CONTEXT_PATH.findall(expression):
            if not any(
                _context_matches(path, prefix) for prefix in TRUSTED_CHECKOUT_CONTEXTS
            ):
                return True
    return False


def _git_call_allowed(verb: str, args: str) -> bool:
    words = args.split()
    if verb in {"fetch", "ls-files", "rev-parse"}:
        return True
    if verb == "cat-file":
        return bool(words) and words[0] == "-e"
    if verb == "diff":
        return "--name-only" in words or "--name-status" in words
    return False


def pull_request_target_violations(workflow: dict[Any, Any], label: str) -> list[str]:
    """Return every place a pull_request_target workflow may materialize PR-head code.

    Fail-closed allowlists rather than a deny-list: checkouts may only name
    trusted contexts, and a job that reads PR refs may only run inert git
    plumbing (fetch, cat-file -e, diff --name-only/--name-status, ls-files,
    rev-parse). This is a regression guard, not a proof of shell semantics.
    """
    if "pull_request_target" not in _triggers(workflow):
        return []
    violations: list[str] = []
    for job_id, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        where = f"{label}:{job_id}"
        job_tainted = _tainted_env(workflow, job)
        if job.get("uses") and _names_pr_ref(job.get("with") or {}, job_tainted):
            violations.append(
                f"{where} passes a PR-head ref to reusable workflow {job['uses']}"
            )
        steps = [step for step in job.get("steps") or [] if isinstance(step, dict)]
        tainted = job_tainted | _tainted_env(*steps)
        # Job-wide: one step can fetch the head and a later step can use
        # FETCH_HEAD or a ref recorded on disk without naming it again.
        reads_pr_refs = bool(tainted) or any(
            FORBIDDEN_REF.search(str(step.get("run") or "")) for step in steps
        )
        for index, step in enumerate(steps):
            step_where = f"{where}:step[{index}] {step.get('name', step.get('uses', ''))}".rstrip()
            uses = str(step.get("uses") or "")
            for field, value in (step.get("with") or {}).items():
                if _names_pr_ref(value, tainted):
                    violations.append(
                        f"{step_where} passes {field}={value!r} to {uses}"
                    )
            if uses.startswith("actions/checkout"):
                for field in ("ref", "repository"):
                    if field in (step.get("with") or {}) and _untrusted_checkout_value(
                        step["with"][field]
                    ):
                        violations.append(
                            f"{step_where} checks out untrusted {field}={step['with'][field]!r}"
                        )
            script = SHELL_COMMENT.sub(
                "", str(step.get("run") or "").replace("\\\n", " ")
            )
            if not script:
                continue
            if GH_CHECKOUT.search(script):
                violations.append(f"{step_where} runs gh pr checkout/diff")
            if not reads_pr_refs:
                continue
            for verb, args in GIT_CALL.findall(script):
                if not _git_call_allowed(verb, args):
                    violations.append(
                        f"{step_where} runs `git {verb}` in a job that reads PR refs"
                    )
            for line in script.splitlines():
                if NETWORK_TOOL.search(line) and _shell_names_pr_ref(line, tainted):
                    violations.append(
                        f"{step_where} downloads PR-head content: {line.strip()}"
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
        # Round-2 adversarial bypasses.
        _prt(
            [{"uses": "actions/checkout@v6", "with": {"ref": "${{ env.HEAD }}"}}],
            env={"HEAD": "${{ github.event.pull_request.head.sha }}"},
        ),
        _prt([
            {
                "uses": "actions/checkout@v6",
                "with": {"ref": "${{ steps.pr.outputs.sha }}"},
            }
        ]),
        _prt([
            {
                "env": {"HEAD": "${{ github.event.pull_request.head.sha }}"},
                "run": 'git fetch origin "$HEAD"\ngit diff "$BASE" "$HEAD" > payload; bash payload',
            }
        ]),
        _prt([
            {
                "env": {"HEAD": "${{ github.event.pull_request.head.sha }}"},
                "run": 'git fetch origin "$HEAD"\ngit cat-file --filters "$HEAD":run.sh | sh',
            }
        ]),
        _prt([
            {
                "env": {"HEAD": "${{ github.event.pull_request.head.sha }}"},
                "run": 'git fetch origin "$HEAD"\ngit \\\n  checkout "$HEAD"',
            }
        ]),
        _prt([
            {
                "env": {"HEAD": "${{ github.event.pull_request.head.sha }}"},
                "run": 'curl -sL "https://codeload.github.com/o/r/tar.gz/$HEAD" | tar -xz',
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
