from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

VALID_TERMINAL_STATUSES = {
    "PRODUCTIVE_DIFF_WITH_EVIDENCE",
    "NO_CHANGE_WITH_EVIDENCE",
    "FAILED_TOOLING_OR_CONTEXT",
}

VERIFIER_VERDICTS = {
    "VERIFIED_PRODUCTIVE_CANDIDATE_DIFF",
    "VERIFIED_NO_CHANGE_WITH_EVIDENCE",
    "VERIFIED_TOOLING_OR_CONTEXT_FAILURE",
    "INVALID_TERMINAL_RETURN",
    "CLAIMS_EXCEED_EVIDENCE",
}

POSITIVE_VERIFIER_VERDICTS = {
    "VERIFIED_PRODUCTIVE_CANDIDATE_DIFF",
    "VERIFIED_NO_CHANGE_WITH_EVIDENCE",
}

NON_CLAIMS = [
    "not deployed",
    "not live",
    "not accepted",
    "not merged unless independently verified",
    "not merge evidence unless PR/CI/merge state are independently verified",
    "no branch-protection mutation",
    "no production cron",
    "no external notification delivery without separate authorization",
    "QWEN35 remains candidate-only",
]

OPERATOR_RECEIPT_NON_CLAIMS = [
    "not merged",
    *NON_CLAIMS,
]

QWEN35_SAFE_TASK_CLASSES: dict[str, tuple[str, ...]] = {
    "scout work": ("stale", "duplicated", "dead end", "dead-link", "inventory", "scan"),
    "reproducer work": ("repro", "reproducer", "flake", "test failure", "cli issue", "sandbox"),
    "test hygiene": ("regression test", "fixture", "smoke", "canary", "test hygiene"),
    "docs/runbook maintenance": ("docs", "documentation", "runbook", "readme", "receipt"),
    "evidence assistant work": ("evidence", "status", "diff summary", "nonclaim", "non-claim", "verification matrix"),
    "safe scaffolds": ("template", "validator", "local-only", "utility", "scaffold"),
    "triage lane work": ("triage", "blocker", "classify", "tooling", "base-inherited"),
    "local automation apprentice work": ("preflight", "registry", "log parsing", "watcher", "launch wrapper"),
}

QWEN35_FORBIDDEN_TASK_CLASSES: dict[str, tuple[str, ...]] = {
    "protected runtime changes": ("protected runtime", "customer behavior", "live behavior"),
    "deployments": ("deploy", "deployment", "release to prod", "production rollout"),
    "branch protection": ("branch protection", "ruleset", "required status check"),
    "production cron": ("production cron", "prod cron", "scheduler"),
    "financial/security-sensitive logic": ("billing", "payment", "financial", "auth", "security", "rls", "credential"),
    "merge/acceptance authority": ("merge", "accept", "accepted", "approve", "mark live"),
}

QWEN35_BROAD_TASK_TERMS = (
    "whole",
    "entire",
    "all repos",
    "broad refactor",
    "refactor the whole",
    "rewrite",
    "re-architect",
    "architecture",
)

QWEN35_TASK_RECIPES: dict[str, dict[str, Any]] = {
    "docs_link_check": {
        "allowed_commands": ["git status --porcelain", "find docs -type f", "python -m pytest tests -q"],
        "allowed_changed_file_globs": ["docs/**", "*.md", "**/*.md"],
        "verifier_rule": "docs-only diff plus post-run verifier; no external link mutation claims without receipt",
    },
    "test_inventory_summary": {
        "allowed_commands": ["git status --porcelain", "find tests -type f", "python -m pytest --collect-only -q"],
        "allowed_changed_file_globs": ["docs/**", "tests/**", "*.md", "**/*.md"],
        "verifier_rule": "inventory summary only unless tests/docs diff is independently observed",
    },
    "flake_reproducer": {
        "allowed_commands": ["git status --porcelain", "python -m pytest -q", "python -m pytest -vv"],
        "allowed_changed_file_globs": ["tests/**", "fixtures/**", "docs/**", "*.md", "**/*.md"],
        "verifier_rule": "reproducer evidence must include exact command and failure/pass output sample",
    },
    "worktree_hygiene_report": {
        "allowed_commands": ["git status --porcelain", "git rev-parse HEAD", "git diff --stat"],
        "allowed_changed_file_globs": ["docs/**", "*.md", "**/*.md"],
        "verifier_rule": "prefer no-change receipt; any mutation must be docs-only",
    },
    "receipt_nonclaim_audit": {
        "allowed_commands": ["git status --porcelain", "git rev-parse HEAD", "rg -n deployed|live|accepted|merged"],
        "allowed_changed_file_globs": ["docs/**", "*.md", "**/*.md"],
        "verifier_rule": "receipt edits must preserve non-claims and avoid merge/live/acceptance assertions",
    },
    "runbook_gap_scan": {
        "allowed_commands": ["git status --porcelain", "find . -name '*runbook*' -o -name '*README*'", "rg -n TODO|FIXME|gap"],
        "allowed_changed_file_globs": ["docs/**", "skills/**", "*.md", "**/*.md"],
        "verifier_rule": "gap scan can propose or docs-patch only; no runtime authority claims",
    },
    "fixture_sanity_check": {
        "allowed_commands": ["git status --porcelain", "find tests -type f", "python -m pytest tests -q"],
        "allowed_changed_file_globs": ["tests/**", "fixtures/**"],
        "verifier_rule": "fixture-only or test-only diff; no production runtime mutation",
    },
    "dead_script_candidate_scan": {
        "allowed_commands": ["git status --porcelain", "find scripts -type f", "rg -n scripts/"],
        "allowed_changed_file_globs": ["docs/**", "scripts/**", "*.md", "**/*.md"],
        "verifier_rule": "identify candidates; deletion requires separate human scope",
    },
    "local_cli_smoke_repro": {
        "allowed_commands": ["git status --porcelain", "python -m pytest -q", "qwen --version"],
        "allowed_changed_file_globs": ["docs/**", "tests/**", "neoengine_local/**", "*.md", "**/*.md"],
        "verifier_rule": "local tooling only; no deploy/merge/external notification actions",
    },
}

DEFAULT_RECIPE_FIELDS: dict[str, Any] = {
    "max_wall_time": "10m",
    "max_tool_calls": 20,
    "expected_terminal_statuses": [
        "NO_CHANGE_WITH_EVIDENCE",
        "PRODUCTIVE_DIFF_WITH_EVIDENCE",
        "FAILED_TOOLING_OR_CONTEXT",
    ],
    "forbidden_commands": [
        "git push",
        "gh pr merge",
        "gh pr review --approve",
        "kubectl apply",
        "terraform apply",
        "curl -X POST",
    ],
}

Runner = Callable[[list[str], Path], tuple[int, str, str]]


@dataclass(frozen=True)
class Qwen35LaneConfig:
    repo: str
    org: str
    worktree: str
    branch: str
    model: str
    invocation: list[str]
    objective: str
    max_files_to_inspect: int = 12
    max_files_to_change: int = 3


@dataclass(frozen=True)
class Qwen35LaneStatus:
    repo: str
    worktree: str
    branch: str
    pgid: str
    session: str
    invocation: str
    started_at: str
    elapsed: str
    current_classification: str
    current_evidence_class: str
    latest_log_path: str
    latest_receipt_path: str
    completion_receipt_exists: bool
    repo_diff_exists: bool
    commit_exists: bool
    pr_exists: bool
    tests_claimed: bool
    claimed_tests_independently_verified: bool


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_runner(command: list[str], cwd: Path) -> tuple[int, str, str]:
    completed = subprocess.run(command, cwd=str(cwd), text=True, capture_output=True, timeout=30)
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _matches_any(text: str, vocabulary: dict[str, tuple[str, ...]]) -> list[str]:
    lowered = text.lower()
    return [label for label, terms in vocabulary.items() if any(term in lowered for term in terms)]


def classify_qwen35_task(task_description: str) -> dict[str, Any]:
    """Decide whether a requested task is safe for Qwen35 candidate execution."""

    lowered = task_description.lower()
    matched_red_lines = _matches_any(task_description, QWEN35_FORBIDDEN_TASK_CLASSES)
    matched_safe_classes = _matches_any(task_description, QWEN35_SAFE_TASK_CLASSES)
    broad_terms = [term for term in QWEN35_BROAD_TASK_TERMS if term in lowered]

    if matched_red_lines:
        classification = "QWEN35_FORBIDDEN_PROTECTED_SURFACE"
        allowed = False
        reason = "Task touches protected surfaces outside Qwen35 authority."
    elif broad_terms:
        classification = "QWEN35_BAD_FIT_TOO_BROAD"
        allowed = False
        reason = "Task is too broad for one bounded low-risk Qwen35 lane."
    elif matched_safe_classes:
        classification = "QWEN35_SAFE_LOW_RISK"
        allowed = True
        reason = "Task matches bounded low-risk assistant classes."
    else:
        classification = "QWEN35_NEEDS_HUMAN_SCOPE"
        allowed = False
        reason = "Task does not match a known safe recipe; human/protected scope required."

    return {
        "classification": classification,
        "allowed": allowed,
        "reason": reason,
        "matched_safe_classes": matched_safe_classes,
        "matched_red_lines": matched_red_lines,
        "broad_terms": broad_terms,
        "non_claims_preserved": OPERATOR_RECEIPT_NON_CLAIMS,
    }


def get_qwen35_task_recipe(task_type: str) -> dict[str, Any]:
    if task_type not in QWEN35_TASK_RECIPES:
        raise KeyError(f"unknown Qwen35 task recipe: {task_type}")
    recipe = dict(DEFAULT_RECIPE_FIELDS)
    recipe.update(QWEN35_TASK_RECIPES[task_type])
    recipe["task_type"] = task_type
    recipe["non_claims_preserved"] = OPERATOR_RECEIPT_NON_CLAIMS
    return recipe


def render_operator_receipt(
    *,
    repo: str,
    branch: str,
    head_sha: str,
    task_type: str,
    terminal_status: str,
    changed_files: Iterable[str],
    commands_observed: Iterable[str | list[str]],
    verifier_result: str,
    operator_action_needed: str,
) -> str:
    def render_command(command: str | list[str]) -> str:
        if isinstance(command, str):
            return command
        return shlex.join(command)

    changed = list(changed_files)
    commands = [render_command(command) for command in commands_observed]
    lines = [
        "QWEN35_CANDIDATE_ASSISTANT_RECEIPT",
        "",
        f"repo: {repo}",
        f"branch: {branch}",
        f"head_sha: {head_sha}",
        f"task_type: {task_type}",
        f"terminal_status: {terminal_status}",
        f"changed_files: {', '.join(changed) if changed else 'none'}",
        "commands_observed:",
    ]
    lines.extend([f"- {command}" for command in commands] or ["- none"])
    lines.extend(
        [
            f"verifier_result: {verifier_result}",
            f"operator_action_needed: {operator_action_needed}",
            "non_claims:",
        ]
    )
    lines.extend(f"- {claim}" for claim in OPERATOR_RECEIPT_NON_CLAIMS)
    return "\n".join(lines) + "\n"


def _normalize_repo_path(path: str) -> str:
    normalized = path.strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _path_matches_any(path: str, patterns: Iterable[str]) -> bool:
    normalized = _normalize_repo_path(path)
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in patterns)


def score_qwen35_diff_risk(changed_files: Iterable[str]) -> dict[str, Any]:
    files = [_normalize_repo_path(file) for file in changed_files if file.strip()]
    forbidden_surfaces: dict[str, tuple[str, ...]] = {
        "deployment/production workflow": (
            ".github/workflows/*deploy*",
            ".github/workflows/*release*",
            "**/deploy/**",
            "**/deployment/**",
            "k8s/**",
            "terraform/**",
        ),
        "auth/security path": ("**/auth/**", "**/security/**", "src/auth/**", "src/security/**", "**/*credential*"),
        "billing/financial path": ("**/billing/**", "**/payment/**", "**/financial/**", "src/billing/**"),
        "production cron/scheduler path": ("**/cron/**production*", "**/prod*cron*", "**/scheduler/**"),
        "branch protection/governance mutation": (".github/rulesets/**", "**/branch-protection/**"),
    }
    matched_forbidden = [
        label for label, patterns in forbidden_surfaces.items() if any(_path_matches_any(file, patterns) for file in files)
    ]

    if not files:
        risk = "RISK_0_NO_CHANGE"
        rationale = "No changed files observed."
    elif matched_forbidden:
        risk = "RISK_5_FORBIDDEN_PROTECTED_SURFACE"
        rationale = "Diff touches surfaces forbidden for Qwen35 candidate lanes."
    elif all(_path_matches_any(file, ("docs/**", "*.md", "**/*.md")) for file in files):
        risk = "RISK_1_DOCS_ONLY"
        rationale = "Diff is limited to Markdown/docs files."
    elif all(_path_matches_any(file, ("tests/**", "fixtures/**", "**/fixtures/**")) for file in files):
        risk = "RISK_2_TEST_OR_FIXTURE_ONLY"
        rationale = "Diff is limited to tests or fixtures."
    elif all(_path_matches_any(file, ("neoengine_local/**", "scripts/**", "tools/**", "hermes_cli/**")) for file in files):
        risk = "RISK_3_LOCAL_TOOLING_ONLY"
        rationale = "Diff is limited to local tooling/control-plane assistant surfaces."
    else:
        risk = "RISK_4_RUNTIME_ADJACENT_REQUIRES_REVIEW"
        rationale = "Diff is outside safe docs/tests/local-tooling buckets and needs independent review."

    return {
        "risk": risk,
        "changed_files": files,
        "matched_forbidden_surfaces": matched_forbidden,
        "rationale": rationale,
        "non_claims_preserved": OPERATOR_RECEIPT_NON_CLAIMS,
    }


class Qwen35Registry:
    """Repo-specific invocation registry for Qwen35 candidate lanes.

    The registry intentionally keys by repo. NeoEngine and NeoWealth are allowed
    to have different known-good forms until a fresh per-repo canary proves a
    new invocation works in that exact worktree.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": "qwen35_invocation_registry.v1", "repos": {}}
        return json.loads(self.path.read_text())

    def _write(self, document: dict[str, Any]) -> None:
        write_json(self.path, document)

    def upsert(
        self,
        *,
        repo: str,
        org: str,
        model: str,
        qwen_cli_version: str,
        known_good_invocation: list[str],
        known_bad_invocations: list[list[str]],
        canary_receipt_path: str,
        notes: str,
        verified_at: str | None = None,
    ) -> dict[str, Any]:
        document = self._read()
        entry = {
            "repo": repo,
            "org": org,
            "model": model,
            "qwen_cli_version": qwen_cli_version,
            "known_good_invocation": known_good_invocation,
            "known_bad_invocations": known_bad_invocations,
            "date_last_verified": verified_at or utc_now(),
            "canary_receipt_path": canary_receipt_path,
            "notes": notes,
        }
        document.setdefault("repos", {})[repo] = entry
        self._write(document)
        return entry

    def get(self, repo: str) -> dict[str, Any] | None:
        return self._read().get("repos", {}).get(repo)

    def allowed_invocation(self, repo: str, invocation: list[str]) -> bool:
        entry = self.get(repo)
        if not entry:
            return False
        return entry.get("known_good_invocation") == invocation


def _run_or_fail(runner: Runner, command: list[str], cwd: Path) -> tuple[int, str, str]:
    try:
        return runner(command, cwd)
    except Exception as exc:  # pragma: no cover - defensive for external runners.
        return 99, "", f"{type(exc).__name__}: {exc}"


def _normalize_repo_url(value: str) -> str:
    text = value.strip()
    if text.startswith("git@github.com:"):
        text = text.removeprefix("git@github.com:")
    elif "github.com/" in text:
        text = text.split("github.com/", 1)[1]
    text = text.removesuffix(".git").strip("/")
    return text


def _remote_matches_repo(remote_url: str, repo: str) -> bool:
    return _normalize_repo_url(remote_url) == repo.strip().strip("/")


def run_preflight_canary(
    *,
    repo: str,
    org: str,
    worktree: str | Path,
    model: str,
    invocation: list[str],
    receipt_path: str | Path,
    runner: Runner = default_runner,
    now: str | None = None,
    registry_path: str | Path | None = None,
    enforce_registry: bool = True,
) -> dict[str, Any]:
    root = Path(worktree)
    receipt = Path(receipt_path)
    observed_at = now or utc_now()
    commands: list[dict[str, Any]] = []

    def run(command: list[str]) -> tuple[int, str, str]:
        rc, out, err = _run_or_fail(runner, command, root)
        commands.append({"command": command, "returncode": rc, "stdout": out[:4000], "stderr": err[:4000]})
        return rc, out, err

    base: dict[str, Any] = {
        "schema_version": "qwen35_preflight_canary.v1",
        "repo": repo,
        "org": org,
        "worktree": str(root),
        "model": model,
        "invocation": invocation,
        "observed_at": observed_at,
        "non_claims_preserved": NON_CLAIMS,
        "commands": commands,
        "registry_enforced": enforce_registry,
        "registry_path": str(registry_path) if registry_path else None,
    }

    if enforce_registry:
        if not registry_path:
            return _finish_canary(receipt, base, "FAILED_TOOLING_OR_CONTEXT", "INVOCATION_REGISTRY_REQUIRED", "--registry is required when registry enforcement is enabled")
        registry = Qwen35Registry(registry_path)
        entry = registry.get(repo)
        base["registry_entry_found"] = entry is not None
        base["registry_known_good_invocation"] = entry.get("known_good_invocation") if entry else None
        if not registry.allowed_invocation(repo, invocation):
            return _finish_canary(receipt, base, "FAILED_TOOLING_OR_CONTEXT", "INVOCATION_NOT_ALLOWED_FOR_REPO", "invocation does not match the repo-specific known-good registry entry")

    rc, repo_root, err = run(["git", "rev-parse", "--show-toplevel"])
    if rc != 0:
        return _finish_canary(receipt, base, "FAILED_TOOLING_OR_CONTEXT", "REPO_ROOT_DETECTION_FAILED", err)
    rc, remote_url, err = run(["git", "config", "--get", "remote.origin.url"])
    if rc != 0 or not remote_url.strip():
        return _finish_canary(receipt, base, "FAILED_TOOLING_OR_CONTEXT", "REMOTE_REPO_DETECTION_FAILED", err or remote_url)
    base["remote_origin_url"] = remote_url
    if not _remote_matches_repo(remote_url, repo):
        return _finish_canary(receipt, base, "FAILED_TOOLING_OR_CONTEXT", "REPO_REMOTE_MISMATCH", "declared repo does not match worktree remote.origin.url")
    rc, branch, err = run(["git", "branch", "--show-current"])
    if rc != 0:
        return _finish_canary(receipt, base, "FAILED_TOOLING_OR_CONTEXT", "BRANCH_DETECTION_FAILED", err)
    rc, head, err = run(["git", "rev-parse", "HEAD"])
    if rc != 0:
        return _finish_canary(receipt, base, "FAILED_TOOLING_OR_CONTEXT", "BASE_SHA_DETECTION_FAILED", err)
    rc, status, err = run(["git", "status", "--porcelain"])
    if rc != 0:
        return _finish_canary(receipt, base, "FAILED_TOOLING_OR_CONTEXT", "WORKTREE_STATUS_FAILED", err)

    base.update({"repo_root": repo_root, "branch": branch, "base_sha": head, "worktree_status": status})
    if status.strip():
        return _finish_canary(receipt, base, "FAILED_TOOLING_OR_CONTEXT", "DIRTY_WORKTREE_REFUSED", status)

    rc, qwen_version, _ = run(["qwen", "--version"])
    base["qwen_cli_version"] = qwen_version if rc == 0 and qwen_version else "unavailable"

    # OpenAI-compatible local endpoints usually expose /v1/models. This proves
    # the endpoint path responds without sending task content or secrets.
    rc, endpoint, err = run(["curl", "-fsS", "http://127.0.0.1:11434/v1/models"])
    if rc != 0:
        return _finish_canary(receipt, base, "FAILED_TOOLING_OR_CONTEXT", "MODEL_ENDPOINT_UNAVAILABLE", err)
    base["model_endpoint_response_sample"] = endpoint[:1000]

    rc, files, err = run(["find", ".", "-maxdepth", "2", "-type", "f", "-not", "-path", "*/.git/*"])
    if rc != 0 or not files.strip():
        return _finish_canary(receipt, base, "FAILED_TOOLING_OR_CONTEXT", "MINIMAL_FILE_INSPECTION_FAILED", err or files)
    base["native_file_tools_execute"] = True
    base["minimal_inspection"] = [line.lstrip("./") for line in files.splitlines()[:10]]
    return _finish_canary(receipt, base, "PASS", None, None)


def _finish_canary(
    receipt: Path,
    base: dict[str, Any],
    status: str,
    failure_mode: str | None,
    detail: str | None,
) -> dict[str, Any]:
    payload = dict(base)
    payload.update({"status": status, "failure_mode": failure_mode, "failure_detail": detail})
    write_json(receipt, payload)
    return payload


def classify_anti_loop(log_text: str) -> str | None:
    if any(status in log_text for status in VALID_TERMINAL_STATUSES):
        return None
    lowered = log_text.lower()
    patterns = [
        r"requires user approval",
        r"permission was declined",
        r"tool .* cannot execute in non-interactive mode",
        r"file not found: .*git log",
        r"pip: command not found",
        r"curl: .*failure writing output",
    ]
    if sum(len(re.findall(pattern, lowered)) for pattern in patterns) >= 1:
        return "FAILED_TOOLING_OR_CONTEXT"
    near_empty_lines = [line for line in log_text.splitlines() if len(line.strip()) <= 2]
    if len(near_empty_lines) >= 5:
        return "FAILED_TOOLING_OR_CONTEXT"
    vague_success = ["success!", "done", "no changes needed", "all good"]
    artifact_terms = ["changed_files", "commit_sha", "pr_url", "tests_run", "NO_CHANGE_WITH_EVIDENCE"]
    if any(term in lowered for term in vague_success) and not any(term.lower() in lowered for term in artifact_terms):
        return "FAILED_TOOLING_OR_CONTEXT"
    return None


def build_bounded_prompt(config: Qwen35LaneConfig) -> str:
    objective_lines = [line.strip() for line in config.objective.splitlines() if line.strip()][:5]
    return "\n".join(
        [
            "QWEN35 bounded candidate-lane prompt",
            f"repo: {config.repo}",
            f"org: {config.org}",
            f"worktree: {config.worktree}",
            f"branch: {config.branch}",
            f"model: {config.model}",
            f"invocation: {' '.join(config.invocation)}",
            "objective:",
            *[f"- {line}" for line in objective_lines],
            f"max files to inspect before choosing a target: {config.max_files_to_inspect}",
            f"max files to change: {config.max_files_to_change}",
            "make the smallest useful diff",
            "run the cheapest relevant verification",
            "return only PRODUCTIVE_DIFF_WITH_EVIDENCE, NO_CHANGE_WITH_EVIDENCE, or FAILED_TOOLING_OR_CONTEXT",
            "no broad refactor permission",
            "no duplicate work if the target already landed on main",
            "red lines: no merge; no deploy; no branch-protection mutation; no production cron; no external notifications; no accepted/live/customer-impact claims; no merge evidence claims without independent PR/CI verification",
            "non-claims: " + "; ".join(NON_CLAIMS),
        ]
    )


def render_lane_dashboard(lanes: Iterable[Qwen35LaneStatus], *, generated_at: str | None = None) -> str:
    lines = [
        "# Qwen35 lane status dashboard",
        f"generated_at: {generated_at or utc_now()}",
        "",
        "## Evidence classes",
        "- controlled execution evidence: launch/log/process/worktree state only",
        "- candidate output evidence: completion receipt only",
        "- delivery evidence: independently verified diff/commit/tests/PR artifacts",
        "- merge evidence: independently verified GitHub PR/CI/merge state only",
        "- acceptance evidence: explicit human/frontier acceptance gate only",
        "",
        "## Active Qwen35 lanes",
    ]
    for lane in lanes:
        lines.extend(
            [
                f"- repo: {lane.repo}",
                f"  worktree: {lane.worktree}",
                f"  branch: {lane.branch}",
                f"  PGID/session: {lane.pgid} / {lane.session}",
                f"  invocation: {lane.invocation}",
                f"  started_at: {lane.started_at}",
                f"  elapsed time: {lane.elapsed}",
                f"  current classification: {lane.current_classification}",
                f"  current evidence class: {lane.current_evidence_class}",
                f"  latest log path: {lane.latest_log_path}",
                f"  latest receipt path: {lane.latest_receipt_path}",
                f"  completion receipt: {yesno(lane.completion_receipt_exists)}",
                f"  repo diff exists: {yesno(lane.repo_diff_exists)}",
                f"  commit exists: {yesno(lane.commit_exists)}",
                f"  PR exists: {yesno(lane.pr_exists)}",
                f"  tests claimed: {yesno(lane.tests_claimed)}",
                f"  claimed tests independently verified: {yesno(lane.claimed_tests_independently_verified)}",
            ]
        )
    return "\n".join(lines) + "\n"


def yesno(value: bool) -> str:
    return "yes" if value else "no"



def _completion_text_exceeds_non_claims(value: Any) -> bool:
    allowed_negative = {claim.lower() for claim in NON_CLAIMS}
    positive_terms = ("deployed", "live", "accepted", "merged")

    def walk(item: Any) -> bool:
        if isinstance(item, dict):
            return any(walk(v) for v in item.values())
        if isinstance(item, list):
            return any(walk(v) for v in item)
        if not isinstance(item, str):
            return False
        text = item.strip().lower()
        if not text or text in allowed_negative:
            return False
        return any(term in text for term in positive_terms)

    return walk(value)

def verify_post_run(
    *,
    repo: str,
    worktree: str | Path,
    completion_receipt: str | Path,
    runner: Runner = default_runner,
    now: str | None = None,
) -> dict[str, Any]:
    root = Path(worktree)
    receipt = Path(completion_receipt)
    commands: list[dict[str, Any]] = []

    def run(command: list[str]) -> tuple[int, str, str]:
        rc, out, err = _run_or_fail(runner, command, root)
        commands.append({"command": command, "returncode": rc, "stdout": out[:4000], "stderr": err[:4000]})
        return rc, out, err

    payload: dict[str, Any] = {
        "schema_version": "qwen35_post_run_verifier.v1",
        "repo": repo,
        "worktree": str(root),
        "completion_receipt": str(receipt),
        "verified_at": now or utc_now(),
        "non_claims_preserved": NON_CLAIMS,
        "commands": commands,
        "blockers": [],
    }
    if not receipt.exists():
        payload.update({"verdict": "VERIFIED_TOOLING_OR_CONTEXT_FAILURE", "blockers": ["completion receipt missing"]})
        return payload
    try:
        completion = json.loads(receipt.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        payload.update({"verdict": "INVALID_TERMINAL_RETURN", "blockers": [f"completion receipt is not JSON: {exc}"]})
        return payload
    terminal = completion.get("terminal_status") or completion.get("status")
    payload["terminal_status"] = terminal
    if terminal not in VALID_TERMINAL_STATUSES:
        payload.update({"verdict": "INVALID_TERMINAL_RETURN", "blockers": [f"invalid terminal status: {terminal!r}"]})
        return payload

    status_rc, git_status, status_err = run(["git", "status", "--porcelain"])
    diff_rc, git_diff, diff_err = run(["git", "diff", "--stat"])
    diff_names_rc, git_diff_names, diff_names_err = run(["git", "diff", "--name-only"])
    head_rc, head_sha, head_err = run(["git", "rev-parse", "HEAD"])
    observed_changed_files = [line.strip() for line in git_diff_names.splitlines() if line.strip()]
    payload.update({"git_status": git_status, "git_diff_stat": git_diff, "git_diff_files": observed_changed_files, "head_sha": head_sha})

    blockers: list[str] = []
    if status_rc != 0:
        blockers.append(f"git status failed: {status_err or git_status}")
    if diff_rc != 0:
        blockers.append(f"git diff failed: {diff_err or git_diff}")
    if diff_names_rc != 0:
        blockers.append(f"git diff name inspection failed: {diff_names_err or git_diff_names}")
    if head_rc != 0 or not head_sha:
        blockers.append(f"git HEAD detection failed: {head_err or head_sha}")
    if blockers:
        payload.update({"verdict": "VERIFIED_TOOLING_OR_CONTEXT_FAILURE", "blockers": blockers})
        return payload

    claimed_commit = str(completion.get("commit_sha") or "").strip()
    claimed_changed_files = [str(path).strip() for path in completion.get("changed_files", []) if str(path).strip()] if isinstance(completion.get("changed_files"), list) else []
    claimed_commit_verified = False
    if claimed_commit:
        exists_rc, _, exists_err = run(["git", "cat-file", "-e", f"{claimed_commit}^{{commit}}"])
        if exists_rc != 0:
            blockers.append("commit_sha claimed but commit does not exist")
        resolved_rc, resolved_sha, resolved_err = run(["git", "rev-parse", claimed_commit])
        if resolved_rc != 0 or not resolved_sha:
            blockers.append(f"commit_sha claimed but could not be resolved: {resolved_err or resolved_sha}")
        elif resolved_sha != head_sha:
            blockers.append("commit_sha claimed but does not match observed HEAD")
        show_rc, commit_stat, show_err = run(["git", "show", "--stat", "--oneline", "--name-only", claimed_commit])
        files_rc, commit_files, files_err = run(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", claimed_commit])
        if show_rc != 0 or not commit_stat.strip():
            blockers.append("commit_sha claimed but commit diff was not independently inspected")
        if files_rc != 0:
            blockers.append(f"commit_sha claimed but commit file list could not be inspected: {files_err or commit_files}")
        elif not commit_files.strip():
            blockers.append("commit_sha claimed but commit has no changed files")
        if not blockers:
            claimed_commit_verified = True
            payload["verified_commit_sha"] = resolved_sha
            payload["verified_commit_stat"] = commit_stat
            payload["verified_commit_files"] = [line for line in commit_files.splitlines() if line.strip()]

    claimed_pr = completion.get("pr_url")
    if claimed_pr:
        rc, _, _ = run(["gh", "pr", "view", str(claimed_pr), "--json", "url,state,headRefOid"])
        if rc != 0:
            blockers.append("pr_url claimed but PR was not independently verified")
    if completion.get("tests_run") and not completion.get("test_results"):
        blockers.append("tests_run claimed without test_results")
    if _completion_text_exceeds_non_claims(completion):
        blockers.append("completion text may exceed non-claim ceiling")
    if git_status.strip() and terminal in {"PRODUCTIVE_DIFF_WITH_EVIDENCE", "NO_CHANGE_WITH_EVIDENCE"}:
        blockers.append("dirty worktree status blocks positive verifier outcome")

    if terminal == "FAILED_TOOLING_OR_CONTEXT":
        verdict = "VERIFIED_TOOLING_OR_CONTEXT_FAILURE"
    elif blockers:
        verdict = "CLAIMS_EXCEED_EVIDENCE"
    elif terminal == "NO_CHANGE_WITH_EVIDENCE":
        if not git_status and not git_diff and not claimed_commit and not claimed_pr:
            verdict = "VERIFIED_NO_CHANGE_WITH_EVIDENCE"
        else:
            verdict = "CLAIMS_EXCEED_EVIDENCE"
            blockers.append("NO_CHANGE_WITH_EVIDENCE does not match independently observed artifacts")
    elif terminal == "PRODUCTIVE_DIFF_WITH_EVIDENCE":
        if observed_changed_files:
            if sorted(claimed_changed_files) != sorted(observed_changed_files):
                verdict = "CLAIMS_EXCEED_EVIDENCE"
                blockers.append("PRODUCTIVE_DIFF_WITH_EVIDENCE changed_files do not match independently observed diff files")
            else:
                verdict = "CLAIMS_EXCEED_EVIDENCE"
                blockers.append("PRODUCTIVE_DIFF_WITH_EVIDENCE has uncommitted diff evidence but lacks matching HEAD commit proof")
        elif claimed_commit_verified:
            verdict = "VERIFIED_PRODUCTIVE_CANDIDATE_DIFF"
        else:
            verdict = "CLAIMS_EXCEED_EVIDENCE"
            blockers.append("PRODUCTIVE_DIFF_WITH_EVIDENCE lacks independently verified matching HEAD commit evidence")
    else:
        verdict = "CLAIMS_EXCEED_EVIDENCE"
        blockers.append("terminal status does not match independently observed artifacts")
    payload.update({"verdict": verdict, "blockers": blockers})
    return payload


def render_operator_summary(
    *,
    what_happened: str,
    what_changed: str,
    what_did_not_happen: str,
    evidence_class: str,
    required_next_action: str,
    safe_next_prompt: str,
) -> str:
    return "\n".join(
        [
            "## What happened",
            what_happened,
            "",
            "## What changed",
            what_changed,
            "",
            "## What did not happen",
            what_did_not_happen,
            "",
            "## Evidence class",
            evidence_class,
            "",
            "## Required next human/frontier action",
            required_next_action,
            "",
            "## Safe next prompt, if recycle is needed",
            safe_next_prompt,
        ]
    ) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qwen35 lane experience utilities")
    sub = parser.add_subparsers(dest="command", required=True)
    canary = sub.add_parser("preflight")
    canary.add_argument("--repo", required=True)
    canary.add_argument("--org", required=True)
    canary.add_argument("--worktree", required=True)
    canary.add_argument("--model", required=True)
    canary.add_argument("--invocation", nargs="+", required=True)
    canary.add_argument("--receipt", required=True)
    canary.add_argument("--registry", required=True)
    canary.add_argument("--no-enforce-registry", action="store_true", help="diagnostic override; result states registry enforcement is disabled")
    verify = sub.add_parser("verify")
    verify.add_argument("--repo", required=True)
    verify.add_argument("--worktree", required=True)
    verify.add_argument("--completion-receipt", required=True)
    picker = sub.add_parser("pick-task")
    picker.add_argument("--description", required=True)
    recipe = sub.add_parser("recipe")
    recipe.add_argument("--task-type", required=True, choices=sorted(QWEN35_TASK_RECIPES))
    risk = sub.add_parser("risk")
    risk.add_argument("--changed-file", action="append", default=[])
    args = parser.parse_args(argv)
    if args.command == "preflight":
        result = run_preflight_canary(
            repo=args.repo,
            org=args.org,
            worktree=args.worktree,
            model=args.model,
            invocation=args.invocation,
            receipt_path=args.receipt,
            registry_path=args.registry,
            enforce_registry=not args.no_enforce_registry,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["status"] == "PASS" else 2
    if args.command == "verify":
        result = verify_post_run(repo=args.repo, worktree=args.worktree, completion_receipt=args.completion_receipt)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["verdict"] in POSITIVE_VERIFIER_VERDICTS else 2
    if args.command == "pick-task":
        result = classify_qwen35_task(args.description)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["allowed"] else 2
    if args.command == "recipe":
        result = get_qwen35_task_recipe(args.task_type)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "risk":
        result = score_qwen35_diff_risk(args.changed_file)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2 if result["risk"] == "RISK_5_FORBIDDEN_PROTECTED_SURFACE" else 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
