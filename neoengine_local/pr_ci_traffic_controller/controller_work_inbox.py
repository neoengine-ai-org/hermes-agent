#!/usr/bin/env python3
"""Durable watchdog -> PR/CI controller work-inbox helper.

All mutations are structured JSON temp-file writes with atomic replace and
readback parse/verification. The helper is org-scoped and contains no live
PR/ticket/branch examples.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import tempfile
import time
from typing import Any

SCHEMA = "hermes.pr-ci-controller-work-inbox.v1"
ORGS = {"neoengine", "neowealth"}
PRODUCERS = {"hourly_watchdog", "runtime_proof_gate", "dispatcher", "pr_ci_controller"}
WORK_TYPES = {
    "dispatcher_repair", "lane_ticket_repair", "branch_namespace_collision",
    "runtime_consumer_repair", "ci_pressure_recheck", "replacement_pressure_ticket",
    "ready_to_advance_local_receipt", "other",
}
PRIORITIES = {"critical": 0, "high": 1, "medium": 2, "low": 3}
STATUSES = {"queued", "claimed", "in_progress", "done", "blocked", "deferred", "superseded"}
AUTHORITY = {"tier1_local", "github_write_required", "dispatcher_required", "founder_required", "protected_boundary_required"}
TERMINAL = {"done", "blocked", "superseded"}
DEFAULT_MAX_ATTEMPTS = 3
TRI_STATE = {"YES", "NO", "UNKNOWN"}
BRANCH_COLLISION_EVIDENCE_KEYS = [
    "colliding_ref",
    "requested_ref",
    "collision_type",
    "git_show_ref_evidence",
    "open_pr_uses_colliding_ref",
    "unpushed_commits_present",
    "canonical_checkout_dirty_or_detached",
    "safe_to_archive",
    "archive_candidate_ref",
    "exact_dispatcher_error",
    "evidence_source_paths",
]
BRANCH_COLLISION_TYPES = {"parent_ref_blocks_nested_ref", "nested_ref_blocks_parent_ref", "unknown"}
STATE_DIR = pathlib.Path("/Users/neoengine/.hermes/state/pr-ci-traffic-controller")
INBOX_PATHS = {
    "neoengine": STATE_DIR / "neoengine-controller-work-inbox.json",
    "neowealth": STATE_DIR / "neowealth-controller-work-inbox.json",
}
WORKDIRS = {
    "neoengine": pathlib.Path("/Users/neoengine/workspace/ai-org/neoengine"),
    "neowealth": pathlib.Path("/Users/neoengine/workspace/ai-org/product-orgs/neowealth"),
}


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:72] or "item"


def stable_id(org: str, work_type: str, state_fingerprint: str) -> str:
    digest = hashlib.sha256(f"{org}|{work_type}|{state_fingerprint}".encode()).hexdigest()[:12]
    return f"{org}-{slug(work_type)}-{digest}"


def empty_inbox(org: str) -> dict[str, Any]:
    return {"schema": SCHEMA, "org": org, "items": []}


def atomic_write_json(path: pathlib.Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as tmp:
            json.dump(data, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
        try:
            reread = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"atomic write readback parse failed for {path}: {exc}") from exc
        if reread != data:
            raise RuntimeError(f"atomic write readback verification failed for {path}")
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def validate_item(item: Any, org: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(item, dict):
        return ["item must be object"]
    required = ["work_item_id", "created_at", "updated_at", "producer", "consumer", "org", "repo", "work_type", "priority", "status", "state_fingerprint", "related_prs", "related_tickets", "related_lanes", "blocker", "exact_instruction", "acceptance_criteria", "authority_required", "safe_to_attempt_now", "attempt_count", "max_attempts", "last_attempt_at", "claim_receipt", "completion_receipt", "blocker_receipt", "superseded_by"]
    for key in required:
        if key not in item:
            errors.append(f"missing {key}")
    if item.get("org") != org:
        errors.append(f"item org mismatch: {item.get('org')} != {org}")
    if item.get("consumer") != "pr_ci_controller":
        errors.append("consumer must be pr_ci_controller")
    if item.get("producer") not in PRODUCERS:
        errors.append("invalid producer")
    if item.get("work_type") not in WORK_TYPES:
        errors.append("invalid work_type")
    if item.get("priority") not in PRIORITIES:
        errors.append("invalid priority")
    if item.get("status") not in STATUSES:
        errors.append("invalid status")
    if item.get("authority_required") not in AUTHORITY:
        errors.append("invalid authority_required")
    for key in ("related_prs", "related_tickets", "related_lanes", "acceptance_criteria"):
        if key in item and not isinstance(item.get(key), list):
            errors.append(f"{key} must be list")
    if not isinstance(item.get("attempt_count", 0), int):
        errors.append("attempt_count must be int")
    if item.get("work_type") == "branch_namespace_collision":
        for key in BRANCH_COLLISION_EVIDENCE_KEYS:
            if key not in item:
                errors.append(f"missing branch collision evidence {key}")
        if item.get("collision_type") not in BRANCH_COLLISION_TYPES:
            errors.append("invalid collision_type")
        for key in ("open_pr_uses_colliding_ref", "unpushed_commits_present", "canonical_checkout_dirty_or_detached", "safe_to_archive"):
            if item.get(key) not in TRI_STATE:
                errors.append(f"{key} must be YES/NO/UNKNOWN")
        for key in ("git_show_ref_evidence", "evidence_source_paths"):
            if key in item and not isinstance(item.get(key), list):
                errors.append(f"{key} must be list")
    return errors


def validate_inbox(data: Any, org: str) -> list[str]:
    if not isinstance(data, dict):
        return ["inbox must be object"]
    errors: list[str] = []
    if data.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    if data.get("org") != org:
        errors.append(f"org mismatch: {data.get('org')} != {org}")
    if not isinstance(data.get("items"), list):
        errors.append("items must be list")
    else:
        for idx, item in enumerate(data["items"]):
            for err in validate_item(item, org):
                errors.append(f"items[{idx}]: {err}")
    return errors


def load_inbox(path: pathlib.Path, org: str, *, create: bool = False) -> dict[str, Any]:
    if org not in ORGS:
        return {"ok": False, "error": f"invalid org {org}", "data": None}
    if not path.exists():
        data = empty_inbox(org)
        if create:
            try:
                atomic_write_json(path, data)
            except Exception as exc:
                return {"ok": False, "error": str(exc), "data": None}
        return {"ok": True, "error": None, "data": data}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"malformed controller inbox: {exc}", "data": None}
    errors = validate_inbox(data, org)
    if errors:
        return {"ok": False, "error": "; ".join(errors), "data": data}
    return {"ok": True, "error": None, "data": data}


def default_item(**kwargs: Any) -> dict[str, Any]:
    now = utc_now()
    org = kwargs["org"]
    work_type = kwargs["work_type"]
    fingerprint = kwargs["state_fingerprint"]
    item = {
        "work_item_id": kwargs.get("work_item_id") or stable_id(org, work_type, fingerprint),
        "created_at": kwargs.get("created_at") or now,
        "updated_at": kwargs.get("updated_at") or now,
        "producer": kwargs.get("producer", "hourly_watchdog"),
        "consumer": "pr_ci_controller",
        "org": org,
        "repo": kwargs["repo"],
        "work_type": work_type,
        "priority": kwargs.get("priority", "medium"),
        "status": kwargs.get("status", "queued"),
        "state_fingerprint": fingerprint,
        "related_prs": list(kwargs.get("related_prs") or []),
        "related_tickets": list(kwargs.get("related_tickets") or []),
        "related_lanes": list(kwargs.get("related_lanes") or []),
        "blocker": kwargs.get("blocker", ""),
        "exact_instruction": kwargs.get("exact_instruction", ""),
        "acceptance_criteria": list(kwargs.get("acceptance_criteria") or []),
        "authority_required": kwargs.get("authority_required", "tier1_local"),
        "safe_to_attempt_now": bool(kwargs.get("safe_to_attempt_now", True)),
        "attempt_count": int(kwargs.get("attempt_count", 0)),
        "max_attempts": int(kwargs.get("max_attempts", DEFAULT_MAX_ATTEMPTS)),
        "last_attempt_at": kwargs.get("last_attempt_at"),
        "claim_receipt": kwargs.get("claim_receipt"),
        "completion_receipt": kwargs.get("completion_receipt"),
        "blocker_receipt": kwargs.get("blocker_receipt"),
        "superseded_by": kwargs.get("superseded_by"),
    }
    if work_type == "branch_namespace_collision":
        item.update({
            "colliding_ref": kwargs.get("colliding_ref") or "UNKNOWN",
            "requested_ref": kwargs.get("requested_ref") or "UNKNOWN",
            "collision_type": kwargs.get("collision_type") or "unknown",
            "git_show_ref_evidence": list(kwargs.get("git_show_ref_evidence") or []),
            "open_pr_uses_colliding_ref": kwargs.get("open_pr_uses_colliding_ref") or "UNKNOWN",
            "unpushed_commits_present": kwargs.get("unpushed_commits_present") or "UNKNOWN",
            "canonical_checkout_dirty_or_detached": kwargs.get("canonical_checkout_dirty_or_detached") or "UNKNOWN",
            "safe_to_archive": kwargs.get("safe_to_archive") or "UNKNOWN",
            "archive_candidate_ref": kwargs.get("archive_candidate_ref") or "UNKNOWN",
            "exact_dispatcher_error": kwargs.get("exact_dispatcher_error") or kwargs.get("blocker", ""),
            "evidence_source_paths": list(kwargs.get("evidence_source_paths") or []),
        })
    return item


def upsert_work_item(path: pathlib.Path, **kwargs: Any) -> dict[str, Any]:
    org = kwargs["org"]
    loaded = load_inbox(path, org, create=True)
    if not loaded["ok"]:
        raise RuntimeError(loaded["error"])
    inbox = loaded["data"]
    item = default_item(**kwargs)
    items = inbox["items"]
    existing = next((it for it in items if it.get("work_type") == item["work_type"] and it.get("state_fingerprint") == item["state_fingerprint"] and it.get("org") == org), None)
    if existing:
        if existing.get("status") in TERMINAL and existing.get("state_fingerprint") == item["state_fingerprint"]:
            return existing
        created_at = existing.get("created_at") or item["created_at"]
        existing.update(item)
        existing["created_at"] = created_at
        existing["updated_at"] = utc_now()
        out = existing
    else:
        items.append(item)
        out = item
    errors = validate_inbox(inbox, org)
    if errors:
        raise RuntimeError("invalid inbox after upsert: " + "; ".join(errors))
    atomic_write_json(path, inbox)
    return out


def run_git(workdir: pathlib.Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(workdir), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)


def parse_collision_ref(item: dict[str, Any]) -> str | None:
    """Return only the structured colliding_ref.

    Branch namespace collision repair must not fall back from vague prose. The
    watchdog producer must provide concrete structured evidence; otherwise
    evaluate_branch_namespace_collision blocks with
    BRANCH_NAMESPACE_COLLISION_REQUIRES_OPERATOR.
    """
    structured = item.get("colliding_ref")
    if isinstance(structured, str) and structured.startswith("refs/heads/"):
        return structured
    return None

def unknown_branch_evidence_fields(item: dict[str, Any]) -> list[str]:
    unknown: list[str] = []
    for key in BRANCH_COLLISION_EVIDENCE_KEYS:
        value = item.get(key)
        if value in (None, "", "UNKNOWN") or (key in {"git_show_ref_evidence", "evidence_source_paths"} and not value):
            unknown.append(key)
    if item.get("collision_type") == "unknown":
        unknown.append("collision_type")
    return sorted(set(unknown))


def branch_evidence_safe_direction_failures(item: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    colliding = item.get("colliding_ref")
    requested = item.get("requested_ref")
    if not (isinstance(colliding, str) and colliding.startswith("refs/heads/")):
        failures.append("colliding_ref is not a local branch ref")
    if not (isinstance(requested, str) and requested.startswith("refs/heads/")):
        failures.append("requested_ref is not a local branch ref")
    if item.get("open_pr_uses_colliding_ref") != "NO":
        failures.append("open PR head-branch safety is not NO")
    if item.get("unpushed_commits_present") != "NO":
        failures.append("unpushed commit safety is not NO")
    if item.get("canonical_checkout_dirty_or_detached") != "YES":
        failures.append("canonical checkout dirty/detached inspection precondition is not YES")
    if item.get("safe_to_archive") != "YES":
        failures.append("safe_to_archive is not YES")
    archive = item.get("archive_candidate_ref")
    if not (isinstance(archive, str) and archive.startswith("refs/heads/archive/controller-work-inbox/")):
        failures.append("archive_candidate_ref is not in the controller archive namespace")
    return failures

def evaluate_branch_namespace_collision(item: dict[str, Any], workdir: pathlib.Path | None) -> dict[str, Any]:
    unknown = unknown_branch_evidence_fields(item)
    if unknown:
        return {"safe": False, "blocker": "branch namespace collision evidence has UNKNOWN fields: " + ", ".join(unknown), "checks": {"unknown_evidence_fields": unknown}}
    direction_failures = branch_evidence_safe_direction_failures(item)
    if direction_failures:
        return {"safe": False, "blocker": "; ".join(direction_failures), "checks": {"structured_evidence": {key: item.get(key) for key in BRANCH_COLLISION_EVIDENCE_KEYS}}}
    if workdir is None:
        return {"safe": False, "blocker": "no workdir supplied for clean ref inspection", "checks": {}}
    if not (workdir / ".git").exists():
        return {"safe": False, "blocker": f"workdir is not a git checkout: {workdir}", "checks": {}}
    old_ref = parse_collision_ref(item)
    if not old_ref:
        return {"safe": False, "blocker": "could not derive colliding local ref from structured evidence", "checks": {}}
    requested_ref = item.get("requested_ref")
    if not (isinstance(requested_ref, str) and requested_ref.startswith("refs/heads/")):
        return {"safe": False, "blocker": "requested_ref is not a local branch ref", "checks": {"requested_ref": requested_ref}}
    rev = run_git(workdir, ["rev-parse", "--verify", old_ref])
    if rev.returncode != 0:
        return {"safe": False, "blocker": f"colliding ref not found locally: {old_ref}", "checks": {"old_ref_exists": False}}
    show_ref = run_git(workdir, ["show-ref", "--verify", old_ref])
    if show_ref.returncode != 0:
        return {"safe": False, "blocker": f"git show-ref could not verify colliding ref: {old_ref}", "checks": {"git_show_ref": show_ref.stderr.strip() or show_ref.stdout.strip()}}
    symbolic = run_git(workdir, ["symbolic-ref", "-q", "HEAD"])
    current_ref = symbolic.stdout.strip() if symbolic.returncode == 0 else None
    branches = run_git(workdir, ["branch", "-r", "--contains", rev.stdout.strip()])
    has_remote_contains = bool(branches.stdout.strip())
    checks = {
        "structured_evidence": {key: item.get(key) for key in BRANCH_COLLISION_EVIDENCE_KEYS},
        "collision_local_ref_only": not has_remote_contains,
        "no_open_pr_head_collision": item.get("open_pr_uses_colliding_ref") == "NO",
        "no_unpushed_unique_commits_would_be_lost": item.get("unpushed_commits_present") == "NO",
        "canonical_checkout_dirty_or_detached_safe_inspection_only": item.get("canonical_checkout_dirty_or_detached") == "YES",
        "branch_can_be_archived": item.get("safe_to_archive") == "YES",
        "old_ref": old_ref,
        "requested_ref": requested_ref,
        "current_ref": current_ref,
        "remote_contains": branches.stdout.strip().splitlines(),
        "git_show_ref_verified": show_ref.stdout.strip(),
    }
    if current_ref == old_ref:
        return {"safe": False, "blocker": "colliding branch is checked out", "checks": checks, "old_ref": old_ref}
    if has_remote_contains:
        return {"safe": False, "blocker": "remote branch contains colliding ref; open PR/unpushed safety not proven locally", "checks": checks, "old_ref": old_ref}
    return {"safe": True, "blocker": None, "checks": checks, "old_ref": old_ref, "old_sha": rev.stdout.strip(), "archive_candidate_ref": item.get("archive_candidate_ref")}


def archive_branch_ref(item: dict[str, Any], workdir: pathlib.Path, evaluation: dict[str, Any]) -> dict[str, Any]:
    old_ref = evaluation["old_ref"]
    name = old_ref.removeprefix("refs/heads/")
    archive = evaluation.get("archive_candidate_ref") or f"refs/heads/archive/controller-work-inbox/{slug(name)}-{int(time.time())}"
    receipt = {
        "completed_at": utc_now(),
        "outcome": "done",
        "action": "branch_namespace_collision_archived_local_ref",
        "old_ref": old_ref,
        "new_archive_ref": archive,
        "old_sha": evaluation.get("old_sha"),
        "safety_checks": evaluation.get("checks"),
        "github_writes_performed": False,
    }
    backup = run_git(workdir, ["update-ref", archive, old_ref])
    if backup.returncode != 0:
        raise RuntimeError(backup.stderr.strip() or backup.stdout.strip())
    delete = run_git(workdir, ["update-ref", "-d", old_ref])
    if delete.returncode != 0:
        raise RuntimeError(delete.stderr.strip() or delete.stdout.strip())
    return receipt


def claim_item(item: dict[str, Any]) -> None:
    now = utc_now()
    item["status"] = "claimed"
    item["updated_at"] = now
    item["last_attempt_at"] = now
    item["attempt_count"] = int(item.get("attempt_count") or 0) + 1
    item["claim_receipt"] = {"claimed_at": now, "consumer": "pr_ci_controller", "github_writes_performed": False}


def process_item(item: dict[str, Any], org: str, workdir: pathlib.Path | None, *, branch_repair_enabled: bool = True) -> str:
    if item.get("authority_required") != "tier1_local":
        item["status"] = "blocked"
        item["blocker_receipt"] = {"blocked_at": utc_now(), "outcome": "blocked", "blocker": f"authority required: {item.get('authority_required')}", "authority_required": item.get("authority_required"), "github_writes_performed": False}
        return "blocked"
    if not item.get("safe_to_attempt_now", True):
        item["status"] = "deferred"
        item["blocker_receipt"] = {"deferred_at": utc_now(), "outcome": "deferred", "reason": "safe_to_attempt_now is false", "github_writes_performed": False}
        return "deferred"
    item["status"] = "in_progress"
    if item.get("work_type") == "branch_namespace_collision":
        if not branch_repair_enabled:
            item["status"] = "blocked"
            item["blocker_receipt"] = {"blocked_at": utc_now(), "outcome": "blocked", "classification": "BRANCH_NAMESPACE_COLLISION_REQUIRES_OPERATOR", "blocker": "branch repair disabled for this tick", "github_writes_performed": False}
            return "blocked"
        ev = evaluate_branch_namespace_collision(item, workdir)
        if not ev.get("safe"):
            item["status"] = "blocked"
            item["blocker_receipt"] = {"blocked_at": utc_now(), "outcome": "blocked", "classification": "BRANCH_NAMESPACE_COLLISION_REQUIRES_OPERATOR", "blocker": ev.get("blocker"), "safety_checks": ev.get("checks"), "github_writes_performed": False}
            return "blocked"
        item["completion_receipt"] = archive_branch_ref(item, workdir or pathlib.Path.cwd(), ev)
        item["status"] = "done"
        return "done"
    # Tier-1 local non-branch items are durable receipt work: acknowledge and
    # classify locally; GitHub/protected movements remain outside this helper.
    item["completion_receipt"] = {"completed_at": utc_now(), "outcome": "done", "action": f"{item.get('work_type')}_local_receipt_written", "instruction_seen": item.get("exact_instruction"), "github_writes_performed": False}
    item["status"] = "done"
    return "done"


def consume_inbox(path: pathlib.Path, org: str, *, max_items: int = 5, workdir: pathlib.Path | None = None, branch_repair_enabled: bool = True) -> dict[str, Any]:
    loaded = load_inbox(path, org, create=True)
    if not loaded["ok"]:
        return {"ok": False, "error": loaded["error"], "done": 0, "blocked": 0, "deferred": 0, "superseded": 0, "queued": 0, "top_action": "fail_closed", "next_item": None}
    inbox = loaded["data"]
    counts = {"done": 0, "blocked": 0, "deferred": 0, "superseded": 0, "queued": 0}
    eligible = [it for it in inbox["items"] if it.get("status") == "queued"]
    eligible.sort(key=lambda it: (PRIORITIES.get(it.get("priority"), 9), it.get("created_at") or ""))
    top_action = "none"
    for idx, item in enumerate(eligible):
        if idx >= max_items:
            item["blocker_receipt"] = {"noted_at": utc_now(), "outcome": "queued", "reason": "budget exhausted before claim", "github_writes_performed": False}
            counts["queued"] += 1
            continue
        claim_item(item)
        outcome = process_item(item, org, workdir, branch_repair_enabled=branch_repair_enabled)
        item["updated_at"] = utc_now()
        counts[outcome] = counts.get(outcome, 0) + 1
        if top_action == "none":
            top_action = f"{item.get('work_item_id')} {outcome}"
    for item in inbox["items"]:
        if item.get("status") == "queued" and item not in eligible:
            counts["queued"] += 1
    errors = validate_inbox(inbox, org)
    if errors:
        return {"ok": False, "error": "; ".join(errors), **counts, "top_action": top_action, "next_item": None}
    atomic_write_json(path, inbox)
    status_counts = {"done": 0, "blocked": 0, "deferred": 0, "superseded": 0, "queued": 0}
    for item in inbox["items"]:
        status = item.get("status")
        if status in status_counts:
            status_counts[status] += 1
    next_item = next((it.get("work_item_id") for it in sorted(inbox["items"], key=lambda it: (PRIORITIES.get(it.get("priority"), 9), it.get("created_at") or "")) if it.get("status") == "queued"), None)
    return {"ok": True, **status_counts, "processed_this_tick": counts, "top_action": top_action, "next_item": next_item, "path": str(path)}


def cli() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("upsert")
    up.add_argument("--org", required=True, choices=sorted(ORGS))
    up.add_argument("--repo", required=True)
    up.add_argument("--producer", default="hourly_watchdog", choices=sorted(PRODUCERS))
    up.add_argument("--work-type", required=True, choices=sorted(WORK_TYPES))
    up.add_argument("--priority", default="medium", choices=sorted(PRIORITIES))
    up.add_argument("--state-fingerprint", required=True)
    up.add_argument("--blocker", required=True)
    up.add_argument("--exact-instruction", required=True)
    up.add_argument("--authority-required", default="tier1_local", choices=sorted(AUTHORITY))
    up.add_argument("--path", type=pathlib.Path)
    up.add_argument("--colliding-ref")
    up.add_argument("--requested-ref")
    up.add_argument("--collision-type", choices=sorted(BRANCH_COLLISION_TYPES))
    up.add_argument("--git-show-ref-evidence", action="append", default=[])
    up.add_argument("--open-pr-uses-colliding-ref", choices=sorted(TRI_STATE))
    up.add_argument("--unpushed-commits-present", choices=sorted(TRI_STATE))
    up.add_argument("--canonical-checkout-dirty-or-detached", choices=sorted(TRI_STATE))
    up.add_argument("--safe-to-archive", choices=sorted(TRI_STATE))
    up.add_argument("--archive-candidate-ref")
    up.add_argument("--exact-dispatcher-error")
    up.add_argument("--evidence-source-path", action="append", default=[])
    con = sub.add_parser("consume")
    con.add_argument("--org", required=True, choices=sorted(ORGS))
    con.add_argument("--path", type=pathlib.Path)
    con.add_argument("--max-items", type=int, default=5)
    con.add_argument("--workdir", type=pathlib.Path)
    con.add_argument("--no-branch-repair", action="store_true")
    args = parser.parse_args()
    if args.cmd == "upsert":
        path = args.path or INBOX_PATHS[args.org]
        item = upsert_work_item(
            path, org=args.org, repo=args.repo, producer=args.producer,
            work_type=args.work_type, priority=args.priority,
            state_fingerprint=args.state_fingerprint, blocker=args.blocker,
            exact_instruction=args.exact_instruction, authority_required=args.authority_required,
            colliding_ref=args.colliding_ref, requested_ref=args.requested_ref,
            collision_type=args.collision_type, git_show_ref_evidence=args.git_show_ref_evidence,
            open_pr_uses_colliding_ref=args.open_pr_uses_colliding_ref,
            unpushed_commits_present=args.unpushed_commits_present,
            canonical_checkout_dirty_or_detached=args.canonical_checkout_dirty_or_detached,
            safe_to_archive=args.safe_to_archive, archive_candidate_ref=args.archive_candidate_ref,
            exact_dispatcher_error=args.exact_dispatcher_error, evidence_source_paths=args.evidence_source_path,
        )
        print(json.dumps({"ok": True, "path": str(path), "work_item_id": item["work_item_id"], "status": item["status"], "authority_required": item["authority_required"], "next_consumer": "PR/CI controller"}, indent=2, sort_keys=True))
        return 0
    path = args.path or INBOX_PATHS[args.org]
    summary = consume_inbox(path, args.org, max_items=args.max_items, workdir=args.workdir or WORKDIRS.get(args.org), branch_repair_enabled=not args.no_branch_repair)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(cli())
