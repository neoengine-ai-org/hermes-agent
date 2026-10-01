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
SHELL_SEPARATOR = re.compile(r"[;&|()\n`]|\$\(")
GH_CHECKOUT = re.compile(r"(?<![\w./-])gh\s+pr\s+(?:checkout|diff)\b")
# Step actions a pull_request_target job may use. Anything else could act on
# PR-head refs it receives through inherited env or the event payload, so an
# unlisted remote action fails closed; local actions are inspected instead.
ALLOWED_ACTIONS = (
    "actions/checkout@",
    "actions/github-script@",
    "actions/cache/restore@",
    "actions/cache/save@",
    "actions/upload-artifact@",
    "actions/download-artifact@",
    "actions/create-github-app-token@",
    "astral-sh/setup-uv@",
)
SCRIPT_PROCESS = re.compile(
    r"child_process|\bexec\.(?:exec|getExecOutput)\b|\bspawn(?:Sync)?\("
)
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


# Inert git plumbing a job that reads PR refs may run, with the exact options
# each verb may take. Anything else (other verbs, other options such as
# --patch, --upload-pack, --filters, or global --work-tree/--git-dir) fails.
GIT_ALLOWED_OPTIONS = {
    "fetch": {"--no-tags", "--no-recurse-submodules", "--quiet", "-q", "--prune"},
    "cat-file": {"-e"},
    "diff": {
        "--no-ext-diff",
        "--no-textconv",
        "--name-only",
        "--name-status",
        "--no-renames",
        "-z",
    },
    "ls-files": {"-z"},
    "rev-parse": {"--verify", "-q", "--quiet"},
    # The exact read-only cleanliness query the receipt validator uses.
    "status": {"--porcelain=v1", "--untracked-files=all"},
}
GIT_GLOBAL_WITH_VALUE = {"-C"}
# git reached indirectly, where the verb cannot be checked statically.
INDIRECT_GIT = re.compile(
    r"(?:command\s+-v|which|type\s+-p)\s+git\b|=\s*[\"']?(?:\S*/)?git[\"']?(?=[\s;&|)]|$)"
)


def _git_violations(script: str) -> list[str]:
    """Return disallowed git invocations, parsed token by token per command."""
    problems: list[str] = []
    for command in SHELL_SEPARATOR.split(script):
        tokens = [token.strip("\"'") for token in command.split()]
        for start, token in enumerate(tokens):
            if token in {"command", "exec", "xargs", "env", "sudo"}:
                continue
            if token.rsplit("/", 1)[-1] != "git":
                continue
            index = start + 1
            while index < len(tokens) and tokens[index].startswith("-"):
                if tokens[index] in GIT_GLOBAL_WITH_VALUE:
                    index += 2
                elif tokens[index] == "--no-pager":
                    index += 1
                else:
                    problems.append(f"git global option {tokens[index]}")
                    index = len(tokens)
            if index >= len(tokens):
                break
            verb, args = tokens[index], tokens[index + 1 :]
            allowed = GIT_ALLOWED_OPTIONS.get(verb)
            if allowed is None:
                problems.append(f"git {verb}")
            elif any(arg.startswith("-") and arg not in allowed for arg in args):
                problems.append(f"git {verb} {' '.join(args)}")
            elif verb == "cat-file" and "-e" not in args:
                problems.append(f"git cat-file {' '.join(args)}")
            elif verb == "diff" and not {"--name-only", "--name-status"} & set(args):
                problems.append(f"git diff {' '.join(args)}")
            break
    return problems


def _local_path(root: Path | None, uses: str) -> Path | None:
    if root is None or not uses.startswith("./"):
        return None
    return root / uses.removeprefix("./").split("@", 1)[0]


def _job_violations(
    workflow: dict[Any, Any],
    job: dict[Any, Any],
    where: str,
    root: Path | None,
    seen: set[str],
) -> list[str]:
    violations: list[str] = []
    job_tainted = _tainted_env(workflow, job)
    uses = str(job.get("uses") or "")
    if uses:
        if _names_pr_ref(job.get("with") or {}, job_tainted):
            violations.append(
                f"{where} passes a PR-head ref to reusable workflow {uses}"
            )
        callee_path = _local_path(root, uses)
        if callee_path is None:
            # A remote reusable workflow runs in this privileged context but
            # cannot be inspected here, so it fails closed.
            violations.append(f"{where} calls uninspectable reusable workflow {uses}")
        elif str(callee_path) not in seen:
            seen.add(str(callee_path))
            callee = _load(callee_path) if callee_path.is_file() else {}
            if not callee:
                violations.append(f"{where} calls missing reusable workflow {uses}")
            for callee_id, callee_job in (callee.get("jobs") or {}).items():
                if isinstance(callee_job, dict):
                    violations.extend(
                        _job_violations(
                            callee,
                            callee_job,
                            f"{where}->{uses}:{callee_id}",
                            root,
                            seen,
                        )
                    )
        return violations
    steps = [step for step in job.get("steps") or [] if isinstance(step, dict)]
    # Composite actions run their steps inside this job; inline local ones
    # recursively (A -> B -> checkout is as dangerous as a direct checkout).
    expanded: list[tuple[str, dict[str, Any]]] = []

    def expand(prefix: str, items: list[Any]) -> None:
        for index, step in enumerate(items):
            if not isinstance(step, dict):
                continue
            label = f"{prefix}:step[{index}] {step.get('name', step.get('uses', ''))}".rstrip()
            expanded.append((label, step))
            action_uses = str(step.get("uses") or "")
            action_dir = _local_path(root, action_uses)
            if action_uses.startswith("./") and action_dir is None:
                violations.append(
                    f"{label} uses uninspectable local action {action_uses}"
                )
            if action_dir is None or str(action_dir) in seen:
                continue
            seen.add(str(action_dir))
            manifest = next(
                (
                    action_dir / name
                    for name in ("action.yml", "action.yaml")
                    if (action_dir / name).is_file()
                ),
                None,
            )
            if manifest is None:
                violations.append(f"{label} uses missing local action {action_uses}")
                continue
            runs = _load(manifest).get("runs") or {}
            if runs.get("using") != "composite":
                violations.append(
                    f"{label} uses non-composite local action {action_uses}"
                )
            expand(f"{label}->", list(runs.get("steps") or []))

    expand(where, steps)
    tainted = job_tainted | _tainted_env(*(step for _, step in expanded))
    # The git allowlist applies to every job in a pull_request_target
    # workflow: the head SHA is always reachable through GITHUB_EVENT_PATH,
    # FETCH_HEAD, or job outputs without naming it in this job.
    for label, step in expanded:
        step_uses = str(step.get("uses") or "")
        for field, value in (step.get("with") or {}).items():
            if _names_pr_ref(value, tainted):
                violations.append(f"{label} passes {field}={value!r} to {step_uses}")
        if (
            step_uses
            and not step_uses.startswith("./")
            and not step_uses.startswith(ALLOWED_ACTIONS)
        ):
            violations.append(f"{label} uses unlisted action {step_uses}")
        if step_uses.startswith("actions/github-script@") and SCRIPT_PROCESS.search(
            str((step.get("with") or {}).get("script", ""))
        ):
            violations.append(f"{label} spawns processes from github-script")
        if step_uses.startswith("actions/checkout"):
            for field in ("ref", "repository"):
                if field in (step.get("with") or {}) and _untrusted_checkout_value(
                    step["with"][field]
                ):
                    violations.append(
                        f"{label} checks out untrusted {field}={step['with'][field]!r}"
                    )
        # Comments are scanned too: stripping them is not quote-aware, and a
        # false positive is cheaper than a hidden command.
        script = str(step.get("run") or "").replace("\\\n", " ")
        if not script:
            continue
        if GH_CHECKOUT.search(script):
            violations.append(f"{label} runs gh pr checkout/diff")
        if INDIRECT_GIT.search(script):
            violations.append(f"{label} reaches git indirectly")
        for problem in _git_violations(script):
            violations.append(f"{label} runs `{problem}`")
        for line in script.splitlines():
            if NETWORK_TOOL.search(line) and _shell_names_pr_ref(line, tainted):
                violations.append(f"{label} downloads PR-head content: {line.strip()}")
    return violations


def pull_request_target_violations(
    workflow: dict[Any, Any], label: str, root: Path | None = ROOT
) -> list[str]:
    """Return every place a pull_request_target workflow may materialize PR-head code.

    Fail-closed allowlists rather than a deny-list: checkouts may only name
    trusted contexts; every shell ``run:`` may only invoke git as inert
    plumbing with allowlisted options; step actions come from a fixed
    allowlist and github-script may not spawn processes; local reusable
    workflows and composite actions are scanned recursively and remote
    reusable workflows fail closed. It inspects workflow text only, so it is a
    regression guard rather than a proof of shell or interpreter semantics
    (for example ``python -c`` or ``eval`` that assemble git at run time).
    """
    if "pull_request_target" not in _triggers(workflow):
        return []
    violations: list[str] = []
    for job_id, job in (workflow.get("jobs") or {}).items():
        if isinstance(job, dict):
            violations.extend(
                _job_violations(workflow, job, f"{label}:{job_id}", root, set())
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


_TAINTED_FETCH = {
    "env": {"HEAD": "${{ github.event.pull_request.head.sha }}"},
    "run": 'git fetch --no-tags origin "$HEAD"',
}


@pytest.mark.parametrize(
    "script",
    [
        # Round-3 adversarial and secondary-review bypasses.
        'git diff --name-only --patch "$BASE" "$HEAD" | sed -n \'s/^+//p\' | sh',
        'git --work-tree=w checkout "$HEAD" -- .',
        'git --git-dir=.git show "$HEAD":x.sh | sh',
        "git fetch --upload-pack='sh -c id' origin \"$HEAD\"",
        'git cat-file -p "$HEAD":run.sh | sh',
        'out=$(git diff "$BASE" "$HEAD") && echo "$out" | sh',
        'command git checkout "$HEAD"',
        # Final-head secondary-review bypasses.
        '/usr/bin/git show "$HEAD":x.sh | sh',
        '"$(command -v git)" show "$HEAD":x.sh | sh',
        'G=git; $G show "$HEAD":x.sh | sh',
        'git -c core.hooksPath=h fetch origin "$HEAD"',
        "git status --short --ignore-submodules=none",
        "git status --porcelain=v1 --untracked-files=all --find-renames",
    ],
)
def test_detector_flags_non_plumbing_git_in_pr_ref_jobs(script: str) -> None:
    workflow = _prt([_TAINTED_FETCH, {"run": script}])
    assert pull_request_target_violations(workflow, "synthetic", root=None)


def test_detector_scans_local_reusable_workflows_and_composite_actions(
    tmp_path: Path,
) -> None:
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "materialize.yml").write_text(
        "on: workflow_call\n"
        "jobs:\n"
        "  run:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v6\n"
        "        with:\n"
        "          ref: ${{ github.event.pull_request.head.sha }}\n",
        encoding="utf-8",
    )
    action = tmp_path / ".github/actions/pr"
    action.mkdir(parents=True)
    (action / "action.yml").write_text(
        "runs:\n"
        "  using: composite\n"
        "  steps:\n"
        "    - shell: bash\n"
        "      run: git checkout ${{ github.event.pull_request.head.sha }}\n",
        encoding="utf-8",
    )
    reusable = {
        True: {"pull_request_target": {}},
        "jobs": {"j": {"uses": "./.github/workflows/materialize.yml"}},
    }
    composite = _prt([{"uses": "./.github/actions/pr"}])
    remote = {
        True: {"pull_request_target": {}},
        "jobs": {"j": {"uses": "org/repo/.github/workflows/x.yml@v1"}},
    }
    for workflow in (reusable, composite, remote):
        assert pull_request_target_violations(workflow, "synthetic", root=tmp_path)


@pytest.mark.parametrize(
    "workflow",
    [
        # Final-head review bypasses: no PR-ref expression appears anywhere.
        _prt([
            {
                "run": 'H=$(jq -r .pull_request.head.sha "$GITHUB_EVENT_PATH")\ngit fetch origin "$H"'
            },
            {"run": "git checkout FETCH_HEAD && make"},
        ]),
        _prt(
            [{"uses": "org/checkout-pr@0123456789abcdef0123456789abcdef01234567"}],
            env={"HEAD_SHA": "${{ github.event.pull_request.head.sha }}"},
        ),
        _prt([{"run": 'echo " #"; git checkout "$HEAD"'}]),
        _prt([
            {
                "uses": "actions/github-script@v7",
                "with": {
                    "script": "await exec.exec('git', ['checkout', process.env.H])"
                },
            }
        ]),
    ],
)
def test_detector_fails_closed_without_explicit_pr_refs(
    workflow: dict[Any, Any],
) -> None:
    assert pull_request_target_violations(workflow, "synthetic", root=None)


def test_detector_expands_nested_composite_actions(tmp_path: Path) -> None:
    for name, body in {
        "a": "    - uses: ./.github/actions/b\n",
        "b": (
            "    - uses: actions/checkout@v6\n"
            "      with:\n"
            "        ref: ${{ github.event.pull_request.head.sha }}\n"
        ),
    }.items():
        action = tmp_path / ".github/actions" / name
        action.mkdir(parents=True)
        (action / "action.yml").write_text(
            f"runs:\n  using: composite\n  steps:\n{body}", encoding="utf-8"
        )
    violations = pull_request_target_violations(
        _prt([{"uses": "./.github/actions/a"}]), "synthetic", root=tmp_path
    )
    assert any("checks out untrusted ref" in v for v in violations)
