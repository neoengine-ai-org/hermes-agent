from __future__ import annotations

import argparse
import json
import os
import re
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

NON_CLAIMS = [
    "not deployed",
    "not live",
    "not accepted",
    "not landed unless independently verified",
    "not merge evidence unless PR/CI/merge state are independently verified",
    "no branch-protection mutation",
    "no production cron",
    "no external notification delivery without separate authorization",
]

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
    }

    rc, repo_root, err = run(["git", "rev-parse", "--show-toplevel"])
    if rc != 0:
        return _finish_canary(receipt, base, "FAILED_TOOLING_OR_CONTEXT", "REPO_ROOT_DETECTION_FAILED", err)
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

    _, git_status, _ = run(["git", "status", "--porcelain"])
    _, git_diff, _ = run(["git", "diff", "--stat"])
    _, head_sha, _ = run(["git", "rev-parse", "HEAD"])
    payload.update({"git_status": git_status, "git_diff_stat": git_diff, "head_sha": head_sha})

    blockers: list[str] = []
    claimed_pr = completion.get("pr_url")
    if claimed_pr:
        rc, _, _ = run(["gh", "pr", "view", str(claimed_pr), "--json", "url,state,headRefOid"])
        if rc != 0:
            blockers.append("pr_url claimed but PR was not independently verified")
    if completion.get("tests_run") and not completion.get("test_results"):
        blockers.append("tests_run claimed without test_results")
    if any(term in json.dumps(completion).lower() for term in ["deployed", "live", "accepted"]):
        blockers.append("completion text may exceed non-claim ceiling")

    if terminal == "FAILED_TOOLING_OR_CONTEXT":
        verdict = "VERIFIED_TOOLING_OR_CONTEXT_FAILURE"
    elif blockers:
        verdict = "CLAIMS_EXCEED_EVIDENCE"
    elif terminal == "NO_CHANGE_WITH_EVIDENCE" and not git_status and not git_diff:
        verdict = "VERIFIED_NO_CHANGE_WITH_EVIDENCE"
    elif terminal == "PRODUCTIVE_DIFF_WITH_EVIDENCE" and (git_diff or completion.get("commit_sha")):
        verdict = "VERIFIED_PRODUCTIVE_CANDIDATE_DIFF"
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
    verify = sub.add_parser("verify")
    verify.add_argument("--repo", required=True)
    verify.add_argument("--worktree", required=True)
    verify.add_argument("--completion-receipt", required=True)
    args = parser.parse_args(argv)
    if args.command == "preflight":
        result = run_preflight_canary(
            repo=args.repo,
            org=args.org,
            worktree=args.worktree,
            model=args.model,
            invocation=args.invocation,
            receipt_path=args.receipt,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["status"] == "PASS" else 2
    if args.command == "verify":
        result = verify_post_run(repo=args.repo, worktree=args.worktree, completion_receipt=args.completion_receipt)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["verdict"] in VERIFIER_VERDICTS else 2
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
