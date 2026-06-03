"""Durable cron observability and agent-review ledger helpers."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now
from utils import atomic_replace

SCHEMA_RUN = "hermes.cron-run-record.v1"
SCHEMA_HEALTH = "hermes.cron-job-health.v1"
SCHEMA_IMPROVEMENT = "hermes.cron-improvement-item.v1"

_REQUIRED_RUN_FIELDS = {
    "schema", "run_id", "job_id", "job_name", "org", "repo", "job_type", "schedule",
    "model", "provider", "no_agent", "workdir", "scheduled_at", "started_at", "finished_at",
    "duration_seconds", "exit_code", "status", "posture", "proof_status", "actions_applied",
    "actions_withheld", "handoffs_created", "handoffs_consumed", "tickets_created", "tickets_updated",
    "tickets_blocked", "runtime_status", "pr_pressure_summary", "permission_blockers", "failure_class",
    "noise_score", "usefulness_score", "should_agent_review", "review_reason", "schedule_signal",
    "output_paths", "redactions_applied", "sensitive_fields_removed", "qwen_training_candidate", "created_at",
}

TOKEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("authorization_header", re.compile(r"(?i)(authorization\s*:\s*)(bearer|basic)\s+[^\s]+")),
    ("cookie_header", re.compile(r"(?i)(cookie\s*:\s*)[^\n\r]+")),
    ("token_assignment", re.compile(r"(?i)\b(access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|secret|password|client[_-]?secret)\s*[=:]\s*[^\s,;]+")),
    ("oauth_token", re.compile(r"\b(?:ya29\.|gh[pousr]_|github_pat_|xox[baprs]-|sk-(?:live|test|proj)-|access-sandbox-|access-production-)[A-Za-z0-9_./+=:-]*")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
    ("financial_account_number", re.compile(r"(?i)\b(account[_ -]?(?:number|id)|routing[_ -]?number|plaid[_ -]?(?:account|item))\s*[=:]\s*[A-Za-z0-9_-]+")),
    ("long_digit_sequence", re.compile(r"\b\d{12,19}\b")),
]


def base_dir() -> Path:
    return get_hermes_home() / "state" / "cron-observability"


def _secure_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _secure_dir(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except Exception:
        pass


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _secure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp, path)
        _secure_file(path)
        # Readback verification.
        json.loads(path.read_text(encoding="utf-8"))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _secure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp, path)
        _secure_file(path)
        if path.read_text(encoding="utf-8") != text:
            raise RuntimeError(f"atomic readback verification failed for {path}")
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _secure_dir(path.parent)
    line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    with open(path, "a+", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
        # Read back only the just-appended byte range; full-file reads make
        # high-volume cron backfills quadratic.
        f.seek(max(0, f.tell() - len(line.encode("utf-8")) - 8))
        tail = f.read()
    _secure_file(path)
    last = tail.splitlines()[-1]
    if json.loads(last) != payload:
        raise RuntimeError(f"jsonl readback verification failed for {path}")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def redact_text(text: str | None) -> tuple[str, list[str]]:
    redacted = str(text or "")
    applied: list[str] = []
    for name, pattern in TOKEN_PATTERNS:
        def repl(match: re.Match[str]) -> str:
            val = match.group(0)
            prefix = ""
            if ":" in val and name in {"authorization_header", "cookie_header"}:
                prefix = val.split(":", 1)[0] + ": "
            return prefix + f"[REDACTED:{name}]"
        new = pattern.sub(repl, redacted)
        if new != redacted:
            applied.append(name)
            redacted = new
    return redacted, sorted(set(applied))


def _log_redactions(run_id: str, applied: list[str], source: str | None = None) -> None:
    if not applied:
        return
    append_jsonl(base_dir() / "redaction-log.jsonl", {
        "created_at": _iso_now(),
        "run_id": run_id,
        "source": source,
        "redactions_applied": applied,
    })


def _qwen_quality(record: dict[str, Any]) -> tuple[str, list[str]]:
    """Classify training value without approving examples for training."""
    reasons: list[str] = []
    if record.get("proof_status") == "FAIL" and record.get("status") == "success":
        reasons.append("claimed success but proof failed")
    if record.get("failure_class") in {"malformed_state", "validation_failed", "runtime_unhealthy"}:
        reasons.append(f"structured {record.get('failure_class')} failure")
    if record.get("handoffs_created") and not record.get("handoffs_consumed"):
        reasons.append("handoff created for downstream consumption review")
    if "schedule" in str(record.get("review_reason") or "").lower():
        reasons.append("schedule signal present")
    if record.get("actions_applied") or record.get("actions_withheld"):
        reasons.append("action decision captured")
    if record.get("redactions_applied"):
        reasons.append("redacted sensitive material")
    if record.get("status") != "success" and record.get("review_reason") != "generic captured run":
        reasons.append("failure with reviewable classification")

    if len(reasons) >= 2 or (record.get("proof_status") == "FAIL" and record.get("status") == "success"):
        return "golden", sorted(set(reasons))
    if reasons:
        return "silver", sorted(set(reasons))
    return "junk", ["low-signal repetitive or generic cron output"]


def _qwen_candidate_key(record: dict[str, Any]) -> str:
    material = {
        "job_id": record.get("job_id"),
        "status": record.get("status"),
        "proof_status": record.get("proof_status"),
        "failure_class": record.get("failure_class"),
        "review_reason": record.get("review_reason"),
        "actions_applied": record.get("actions_applied") or [],
        "actions_withheld": record.get("actions_withheld") or [],
        "handoffs_created": record.get("handoffs_created") or [],
        "permission_blockers": record.get("permission_blockers") or [],
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]


def _qwen_candidate_exists(candidate_key: str, run_id: str) -> bool:
    for row in _read_jsonl(base_dir() / "qwen-training-candidates.jsonl"):
        if row.get("candidate_key") == candidate_key or row.get("run_id") == run_id:
            return True
    return False


def _iso_now() -> str:
    return _hermes_now().isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _duration_seconds(started: str, finished: str) -> float:
    s, f = _parse_time(started), _parse_time(finished)
    if not s or not f:
        return 0.0
    return max(0.0, (f - s).total_seconds())


def infer_org(job: dict[str, Any], output: str = "") -> str:
    text = " ".join(str(job.get(k) or "") for k in ("name", "prompt", "script", "workdir")) + " " + output[:2000]
    low = text.lower()
    has_ne = "neoengine" in low
    has_nw = "neowealth" in low
    if has_ne and has_nw:
        return "cross_org"
    if has_ne:
        return "neoengine"
    if has_nw:
        return "neowealth"
    return "unknown"


def infer_job_type(job: dict[str, Any]) -> str:
    text = " ".join(str(job.get(k) or "") for k in ("name", "prompt", "script")).lower()
    if any(x in text for x in ("pr/ci", "pr-ci", "traffic controller", "pr_ci")):
        return "pr_ci_controller"
    if "hourly" in text and "watchdog" in text:
        return "hourly_watchdog"
    if "dispatcher" in text and "heartbeat" in text:
        return "dispatcher_heartbeat"
    if "green" in text and ("merge" in text or "automerge" in text):
        return "green_merge_watchdog"
    if "roadmap" in text or "router" in text:
        return "router"
    if "advisory" in text or "milestone" in text:
        return "advisory"
    return "other"


def _extract_list(output: str, *labels: str) -> list[str]:
    items: list[str] = []
    for label in labels:
        pat = re.compile(rf"(?im)^\s*{re.escape(label)}\s*[:=]\s*(.+)$")
        for m in pat.finditer(output or ""):
            raw = m.group(1).strip()
            if not raw or raw in {"[]", "none", "None", "N/A"}:
                continue
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    items.extend(str(x).strip() for x in parsed if str(x).strip())
                    continue
            except Exception:
                pass
            items.extend(x.strip() for x in re.split(r"[,;]", raw) if x.strip())
    deduped: list[str] = []
    for item in items:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _extract_scalar(output: str, *labels: str, allowed: set[str] | None = None, default: str | None = None) -> str | None:
    for label in labels:
        pat = re.compile(rf"(?im)^\s*{re.escape(label)}\s*[:=]\s*([A-Za-z0-9_.-]+)")
        m = pat.search(output or "")
        if m:
            value = m.group(1).strip()
            if allowed is None or value in allowed:
                return value
    return default


def _schedule_display(job: dict[str, Any]) -> str:
    display = str(job.get("schedule_display") or "").strip()
    if display:
        return display
    schedule = job.get("schedule")
    if isinstance(schedule, dict):
        return str(schedule.get("display") or schedule.get("expr") or schedule.get("run_at") or "")
    return str(schedule or "")


def _infer_status(exit_code: int, output: str) -> str:
    low = (output or "").lower()
    if "fail-closed" in low or "failed_closed" in low:
        return "failed_closed"
    if exit_code != 0:
        return "failed"
    if "partial" in low:
        return "partial"
    if re.search(r"(?im)^\s*(status\s*[:=]\s*)?skipped\s*$", output or ""):
        return "skipped"
    return "success"


def _failure_class(status: str, exit_code: int, output: str) -> str:
    low = (output or "").lower()
    if status == "success":
        return "none"
    if "timeout" in low or "timed out" in low:
        return "timeout"
    if "permission" in low or "403" in low or "forbidden" in low:
        return "permission_limit"
    if "provider" in low or "api" in low or "auth" in low:
        return "provider_error"
    if "runtime" in low and ("fail" in low or "unhealthy" in low):
        return "runtime_unhealthy"
    if "malformed" in low or "json" in low:
        return "malformed_state"
    if "delivery" in low:
        return "delivery_unproven"
    if "validation" in low:
        return "validation_failed"
    if exit_code != 0:
        return "script_error"
    return "unknown"


def _noise_and_usefulness(status: str, output: str, actions: list[str], handoffs: list[str]) -> tuple[str, str, bool, str]:
    low = (output or "").lower()
    no_action = ("[silent]" in low or "nothing to report" in low or "no action" in low) and not actions and not handoffs
    if status != "success":
        return "MEDIUM", "LOW", True, f"cron status is {status}"
    if actions or handoffs:
        return "LOW", "HIGH", False, "useful action or handoff produced"
    if no_action:
        return "LOW", "LOW", False, "no-action tick"
    if len(output or "") > 8000:
        return "HIGH", "MEDIUM", True, "output exceeds compact target"
    return "MEDIUM", "MEDIUM", False, "generic captured run"


def validate_run_record(record: dict[str, Any]) -> list[str]:
    errors = []
    missing = sorted(_REQUIRED_RUN_FIELDS - set(record))
    if missing:
        errors.append(f"missing fields: {', '.join(missing)}")
    if record.get("schema") != SCHEMA_RUN:
        errors.append("schema mismatch")
    if record.get("status") not in {"success", "failed", "failed_closed", "partial", "skipped", "unknown"}:
        errors.append("invalid status")
    if record.get("failure_class") not in {"none", "script_error", "provider_error", "runtime_unhealthy", "malformed_state", "delivery_unproven", "timeout", "permission_limit", "validation_failed", "unknown"}:
        errors.append("invalid failure_class")
    return errors


def build_run_record(*, job: dict[str, Any], scheduled_at: str | None, started_at: str, finished_at: str,
                     exit_code: int, output: str = "", cron_output_path: str | None = None,
                     full_report_path: str | None = None, related_receipts: list[str] | None = None,
                     qwen_training_candidate: bool | None = None) -> dict[str, Any]:
    safe_output, redactions = redact_text(output)
    job_id = str(job.get("id") or "unknown")
    scheduled_at = scheduled_at or started_at
    run_hash = hashlib.sha256(f"{job_id}|{started_at}|{finished_at}|{cron_output_path or ''}|{safe_output[:500]}".encode()).hexdigest()[:16]
    run_id = f"{job_id}-{run_hash}"
    status = _infer_status(exit_code, safe_output)
    actions = _extract_list(safe_output, "ACTIONS_APPLIED", "actions_applied", "Actions applied")
    handoffs_created = _extract_list(safe_output, "HANDOFFS_CREATED", "handoffs_created", "Handoffs created")
    handoffs_consumed = _extract_list(safe_output, "HANDOFFS_CONSUMED", "handoffs_consumed", "Handoffs consumed")
    permission_blockers = _extract_list(safe_output, "PERMISSION_BLOCKERS", "permission_blockers", "Permission blockers")
    noise, useful, review, reason = _noise_and_usefulness(status, safe_output, actions, handoffs_created)
    if permission_blockers:
        review, reason = True, "permission blocker reported"
    proof_status = _extract_scalar(safe_output, "PROOF", "PROOF_STATUS", "proof_status", allowed={"PASS", "FAIL", "N/A", "unknown"}, default="unknown") or "unknown"
    runtime_status = _extract_scalar(safe_output, "RUNTIME", "RUNTIME_STATUS", "runtime_status", allowed={"PASS", "FAIL", "REPAIRED_PASS", "REPAIR_ATTEMPTED_FAILED", "N/A", "unknown"}, default="unknown") or "unknown"
    candidate = bool(qwen_training_candidate) or status != "success" or proof_status == "FAIL" or bool(handoffs_created) or "schedule" in safe_output.lower()
    return {
        "schema": SCHEMA_RUN,
        "run_id": run_id,
        "job_id": job_id,
        "job_name": str(job.get("name") or job_id),
        "org": infer_org(job, safe_output),
        "repo": job.get("repo"),
        "job_type": infer_job_type(job),
        "schedule": _schedule_display(job),
        "model": job.get("model"),
        "provider": job.get("provider"),
        "no_agent": bool(job.get("no_agent")),
        "workdir": job.get("workdir"),
        "scheduled_at": scheduled_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": _duration_seconds(started_at, finished_at),
        "exit_code": int(exit_code),
        "status": status,
        "posture": _extract_scalar(safe_output, "POSTURE", "posture"),
        "proof_status": proof_status,
        "actions_applied": actions,
        "actions_withheld": _extract_list(safe_output, "ACTIONS_WITHHELD", "actions_withheld"),
        "handoffs_created": handoffs_created,
        "handoffs_consumed": handoffs_consumed,
        "tickets_created": _extract_list(safe_output, "TICKETS_CREATED", "tickets_created"),
        "tickets_updated": _extract_list(safe_output, "TICKETS_UPDATED", "tickets_updated"),
        "tickets_blocked": _extract_list(safe_output, "TICKETS_BLOCKED", "tickets_blocked"),
        "runtime_status": runtime_status,
        "pr_pressure_summary": {},
        "permission_blockers": permission_blockers,
        "failure_class": _failure_class(status, exit_code, safe_output),
        "noise_score": noise,
        "usefulness_score": useful,
        "should_agent_review": bool(review),
        "review_reason": reason,
        "schedule_signal": {"too_frequent": False, "too_infrequent": False, "recommended_interval_minutes": None, "rationale": "insufficient rolling evidence"},
        "output_paths": {"cron_output": cron_output_path, "full_report": full_report_path, "related_receipts": related_receipts or []},
        "redactions_applied": redactions,
        "sensitive_fields_removed": redactions,
        "qwen_training_candidate": candidate,
        "created_at": _iso_now(),
    }


def capture_cron_run(**kwargs: Any) -> dict[str, Any]:
    ensure_observability_install()
    record = build_run_record(**kwargs)
    errors = validate_run_record(record)
    if errors:
        raise ValueError("invalid cron run record: " + "; ".join(errors))
    b = base_dir()
    run_path = b / "runs" / record["job_id"] / f"{record['run_id']}.json"
    atomic_write_json(run_path, record)
    append_jsonl(b / "cron-run-ledger.jsonl", record)
    _log_redactions(record["run_id"], record["redactions_applied"], record.get("output_paths", {}).get("cron_output"))
    if record["qwen_training_candidate"]:
        quality_label, quality_reasons = _qwen_quality(record)
        candidate_key = _qwen_candidate_key(record)
        candidate = {
            "schema": "hermes.qwen-training-candidate.v1",
            "created_at": _iso_now(),
            "run_id": record["run_id"],
            "candidate_key": candidate_key,
            "job_id": record["job_id"],
            "job_name": record["job_name"],
            "learning_value": record["review_reason"],
            "quality_label": quality_label,
            "quality_reasons": quality_reasons,
            "status": record["status"],
            "proof_status": record["proof_status"],
            "failure_class": record["failure_class"],
            "evidence_paths": [str(run_path)] + [p for p in [record["output_paths"].get("cron_output")] if p],
            "review_status": "unreviewed",
            "approved_for_training": False,
            "redactions_applied": record["redactions_applied"],
        }
        if not _qwen_candidate_exists(candidate_key, record["run_id"]):
            append_jsonl(b / "qwen-training-candidates.jsonl", candidate)
    return record


def _capture_index_path() -> Path:
    return base_dir() / ".captured-output-index.json"


def _capture_path_index_path() -> Path:
    return base_dir() / ".captured-output-path-index.json"


def _load_index() -> set[str]:
    p = _capture_index_path()
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data.get("hashes", []))
    except Exception:
        return set()


def _save_index(values: set[str]) -> None:
    atomic_write_json(_capture_index_path(), {"hashes": sorted(values), "updated_at": _iso_now()})


def _load_path_index() -> set[str]:
    """Load the fast collector path index, bootstrapping from the ledger.

    The content-hash index is the stronger duplicate guard, but reading tens of
    thousands of already-captured markdown files every collector tick is too
    expensive.  Once a path has a durable run record, the collector can skip it
    before opening the file; central-runner captures still record the exact
    output path in the ledger.
    """
    p = _capture_path_index_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return set(data.get("paths", []))
        except Exception:
            pass

    paths: set[str] = set()
    for record in _load_records():
        try:
            cron_output = (record.get("output_paths") or {}).get("cron_output")
            if cron_output:
                paths.add(str(Path(cron_output).resolve()))
        except Exception:
            continue
    if paths:
        _save_path_index(paths)
    return paths


def _save_path_index(values: set[str]) -> None:
    atomic_write_json(_capture_path_index_path(), {"paths": sorted(values), "updated_at": _iso_now()})


def capture_output_file_once(*, output_path: Path | str, job: dict[str, Any], started_at: str | None = None,
                             finished_at: str | None = None, exit_code: int = 0) -> dict[str, Any] | None:
    output_path = Path(output_path)
    raw = output_path.read_text(encoding="utf-8", errors="replace") if output_path.exists() else ""
    digest = hashlib.sha256((str(output_path.resolve()) + "\0" + raw).encode("utf-8", errors="replace")).hexdigest()
    index = _load_index()
    if digest in index:
        return None
    started_at = started_at or datetime.fromtimestamp(output_path.stat().st_mtime, timezone.utc).isoformat()
    finished_at = finished_at or started_at
    record = capture_cron_run(job=job, scheduled_at=started_at, started_at=started_at, finished_at=finished_at,
                              exit_code=exit_code, output=raw, cron_output_path=str(output_path))
    index.add(digest)
    _save_index(index)
    return record


def _load_jobs(jobs_path: Path | None = None) -> list[dict[str, Any]]:
    jobs_path = jobs_path or (get_hermes_home() / "cron" / "jobs.json")
    try:
        data = json.loads(jobs_path.read_text(encoding="utf-8"))
        return list(data.get("jobs", []))
    except Exception:
        return []


def collect_existing_cron_outputs(*, output_dir: Path | None = None, jobs_path: Path | None = None,
                                  max_new: int | None = None) -> dict[str, Any]:
    ensure_observability_install()
    output_dir = output_dir or (get_hermes_home() / "cron" / "output")
    jobs = {str(j.get("id")): j for j in _load_jobs(jobs_path)}
    index = _load_index()
    path_index = _load_path_index()
    captured = 0
    skipped = 0
    for md in sorted(output_dir.glob("*/*.md")) if output_dir.exists() else []:
        resolved_path = str(md.resolve())
        if resolved_path in path_index:
            skipped += 1
            continue
        job_id = md.parent.name
        job = jobs.get(job_id, {"id": job_id, "name": job_id, "schedule_display": "unknown", "no_agent": False})
        # Infer failure from filename/content only when central runner metadata is unavailable.
        raw = md.read_text(encoding="utf-8", errors="replace")
        digest = hashlib.sha256((str(md.resolve()) + "\0" + raw).encode("utf-8", errors="replace")).hexdigest()
        if digest in index:
            path_index.add(resolved_path)
            skipped += 1
            continue
        text = raw[:4000]
        exit_code = 1 if "(FAILED)" in text or "script failed" in text.lower() or "exited with code" in text.lower() else 0
        started_at = datetime.fromtimestamp(md.stat().st_mtime, timezone.utc).isoformat()
        capture_cron_run(job=job, scheduled_at=started_at, started_at=started_at, finished_at=started_at,
                         exit_code=exit_code, output=raw, cron_output_path=str(md))
        index.add(digest)
        path_index.add(resolved_path)
        captured += 1
        if captured % 1000 == 0:
            _save_index(index)
            _save_path_index(path_index)
        if max_new is not None and captured >= max_new:
            break
    _save_index(index)
    _save_path_index(path_index)
    summarize_cron_observability(jobs=list(jobs.values()))
    generate_schedule_recommendations(jobs=list(jobs.values()))
    generate_agent_review_inbox()
    generate_agent_review_rollup()
    return {"captured": captured, "duplicates_skipped": skipped, "base_dir": str(base_dir())}


def _load_records() -> list[dict[str, Any]]:
    p = base_dir() / "cron-run-ledger.jsonl"
    if not p.exists():
        return []
    records = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            continue
    return records


def _rate(records: list[dict[str, Any]], pred) -> float:
    if not records:
        return 0.0
    return round(sum(1 for r in records if pred(r)) / len(records), 4)


def _p95(vals: list[float]) -> float:
    if not vals:
        return 0.0
    vals = sorted(vals)
    idx = min(len(vals) - 1, math.ceil(0.95 * len(vals)) - 1)
    return vals[idx]


def summarize_cron_observability(*, jobs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    records = _load_records()
    by_job: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_job[str(r.get("job_id"))].append(r)
    known = {str(j.get("id")): j for j in (jobs if jobs is not None else _load_jobs())}
    summaries: dict[str, Any] = {}
    handoff_health = {"schema": "hermes.cron-handoff-health.v1", "updated_at": _iso_now(), "stale_handoffs": [], "by_job": {}}
    now = _hermes_now()
    for job_id, recs in by_job.items():
        recs.sort(key=lambda r: r.get("started_at") or "")
        last = recs[-1]
        failures = [r for r in recs if r.get("status") != "success"]
        consecutive = 0
        for r in reversed(recs):
            if r.get("status") == "success":
                break
            consecutive += 1
        last24 = [r for r in recs if (_parse_time(r.get("started_at")) and (now - _parse_time(r.get("started_at"))).total_seconds() <= 86400)]
        last7 = [r for r in recs if (_parse_time(r.get("started_at")) and (now - _parse_time(r.get("started_at"))).total_seconds() <= 7*86400)]
        durations = [float(r.get("duration_seconds") or 0) for r in recs]
        created = [(r, h) for r in recs for h in r.get("handoffs_created", [])]
        consumed_values = {h for r in recs for h in r.get("handoffs_consumed", [])}
        stale = [h for _r, h in created if h not in consumed_values]
        for h in stale:
            handoff_health["stale_handoffs"].append({"job_id": job_id, "handoff_id": h, "status": "unconsumed"})
        h_created_rate = _rate(recs, lambda r: bool(r.get("handoffs_created")))
        h_consumed_rate = _rate(recs, lambda r: bool(r.get("handoffs_consumed")))
        no_action_rate = _rate(recs, lambda r: not r.get("actions_applied") and not r.get("handoffs_created") and r.get("status") == "success")
        action_rate = _rate(recs, lambda r: bool(r.get("actions_applied") or r.get("handoffs_created")))
        duplicate_noise_rate = _rate(recs, lambda r: r.get("noise_score") == "HIGH" or "same blocker" in json.dumps(r).lower())
        too_frequent = len(recs) >= 5 and no_action_rate >= 0.9
        too_slow = consecutive > 0 and (known.get(job_id, {}).get("schedule", {}).get("kind") == "interval") and consecutive >= 2
        if consecutive >= 3:
            recommendation = "disable until repaired"
        elif too_frequent:
            recommendation = "decrease frequency"
        elif too_slow:
            recommendation = "increase frequency"
        elif stale:
            recommendation = "needs human review"
        else:
            recommendation = "keep schedule"
        summaries[job_id] = {
            "schema": SCHEMA_HEALTH,
            "job_id": job_id,
            "job_name": last.get("job_name"),
            "run_count": len(recs),
            "last_run_at": last.get("started_at"),
            "last_success_at": next((r.get("started_at") for r in reversed(recs) if r.get("status") == "success"), None),
            "last_failure_at": failures[-1].get("started_at") if failures else None,
            "consecutive_failures": consecutive,
            "success_rate_24h": _rate(last24, lambda r: r.get("status") == "success"),
            "success_rate_7d": _rate(last7, lambda r: r.get("status") == "success"),
            "average_duration_seconds": round(sum(durations) / len(durations), 3) if durations else 0,
            "p95_duration_seconds": _p95(durations),
            "action_rate": action_rate,
            "no_action_rate": no_action_rate,
            "failure_closed_rate": _rate(recs, lambda r: r.get("status") == "failed_closed"),
            "duplicate_noise_rate": duplicate_noise_rate,
            "handoff_creation_rate": h_created_rate,
            "handoff_consumption_rate": h_consumed_rate,
            "stale_handoff_count": len(stale),
            "blocked_inbox_count": len([r for r in recs if r.get("permission_blockers")]),
            "average_handoff_created_to_consumed_seconds": None,
            "schedule_usefulness": "too frequent" if too_frequent else ("too slow" if too_slow else "healthy"),
            "recommendation": recommendation,
        }
        handoff_health["by_job"][job_id] = {"stale_handoff_count": len(stale), "created": len(created), "consumed": len(consumed_values)}
    payload = {"schema": SCHEMA_HEALTH, "updated_at": _iso_now(), "jobs": summaries}
    atomic_write_json(base_dir() / "job-health-summary.json", payload)
    atomic_write_json(base_dir() / "handoff-health.json", handoff_health)
    return payload


def generate_schedule_recommendations(*, jobs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    summary_path = base_dir() / "job-health-summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else summarize_cron_observability(jobs=jobs)
    jobs_by_id = {str(j.get("id")): j for j in (jobs if jobs is not None else _load_jobs())}
    recommendations = []
    for job_id, health in summary.get("jobs", {}).items():
        rec = "keep"
        recommended_schedule = None
        rationale = "job cadence appears healthy"
        if health.get("consecutive_failures", 0) >= 3:
            rec, rationale = "disable_until_repaired", "cron has failed repeatedly; repair before more ticks"
        elif health.get("no_action_rate", 0) >= 0.9 and health.get("handoff_consumption_rate", 0) == 0:
            rec, rationale = "decrease_frequency", "90%+ recent ticks are no-op and no downstream consumption is evident"
        elif health.get("stale_handoff_count", 0) > 0:
            rec, rationale = "needs_review", "handoffs are being created but remain unconsumed"
        elif health.get("failure_closed_rate", 0) > 0.5:
            rec, rationale = "increase_frequency", "repair/fail-closed loop may need tighter attention after human review"
        recommendations.append({
            "job_id": job_id,
            "job_name": health.get("job_name") or jobs_by_id.get(job_id, {}).get("name") or job_id,
            "current_schedule": _schedule_display(jobs_by_id.get(job_id, {})) or "unknown",
            "recommendation": rec,
            "recommended_schedule": recommended_schedule,
            "confidence": "HIGH" if rec != "keep" and len(_load_records()) >= 3 else "MEDIUM",
            "rationale": rationale,
            "evidence": [str(base_dir() / "job-health-summary.json")],
            "requires_human_approval": True,
        })
    payload = {"updated_at": _iso_now(), "recommendations": recommendations, "applied_changes": []}
    atomic_write_json(base_dir() / "schedule-recommendations.json", payload)
    return payload


def _improvement_item(job_id: str, job_name: str, org: str, issue_type: str, severity: str, summary: str, evidence_paths: list[str], suggested_fix: str, owner: str) -> dict[str, Any]:
    item_id = hashlib.sha256(f"{job_id}|{issue_type}|{summary}".encode()).hexdigest()[:16]
    return {
        "schema": SCHEMA_IMPROVEMENT,
        "item_id": item_id,
        "created_at": _iso_now(),
        "job_id": job_id,
        "job_name": job_name,
        "org": org,
        "issue_type": issue_type,
        "severity": severity,
        "summary": summary,
        "evidence_paths": evidence_paths,
        "suggested_fix": suggested_fix,
        "owner": owner,
        "status": "queued",
        "reviewed_by": None,
        "resolution": None,
    }


def generate_agent_review_inbox() -> dict[str, Any]:
    summary_path = base_dir() / "job-health-summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else summarize_cron_observability()
    rec_path = base_dir() / "schedule-recommendations.json"
    recs = json.loads(rec_path.read_text(encoding="utf-8")) if rec_path.exists() else generate_schedule_recommendations()
    records_by_job: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in _load_records():
        records_by_job[str(r.get("job_id"))].append(r)
    items: dict[str, dict[str, Any]] = {}
    evidence = [str(base_dir() / "job-health-summary.json"), str(base_dir() / "cron-run-ledger.jsonl")]
    for job_id, health in summary.get("jobs", {}).items():
        latest = records_by_job[job_id][-1] if records_by_job.get(job_id) else {}
        name = health.get("job_name") or latest.get("job_name") or job_id
        org = latest.get("org") or "unknown"
        if health.get("consecutive_failures", 0) >= 3:
            item = _improvement_item(job_id, name, org, "failure", "high", f"{name} has {health['consecutive_failures']} consecutive failures", evidence, "Inspect the failing script/provider path and add deterministic proof before resuming normal cadence.", "cron_maintainer")
            items[item["item_id"]] = item
        if health.get("no_action_rate", 0) >= 0.9 and len(records_by_job.get(job_id, [])) >= 5:
            item = _improvement_item(job_id, name, org, "noise", "medium", f"{name} repeatedly produces no action", evidence, "Review whether cadence should decrease or output should stay silent on routine no-op ticks.", "cron_maintainer")
            items[item["item_id"]] = item
        if health.get("stale_handoff_count", 0) > 0:
            item = _improvement_item(job_id, name, org, "handoff", "high", f"{name} created unconsumed handoffs", [*evidence, str(base_dir() / "handoff-health.json")], "Install/verify consumer receipts for downstream agents and repair stale inbox items.", "dispatcher")
            items[item["item_id"]] = item
        if health.get("blocked_inbox_count", 0) > 0:
            item = _improvement_item(job_id, name, org, "permission_blocker", "medium", f"{name} reports repeated permission blockers", evidence, "Route to human approval without weakening protected gates.", "founder")
            items[item["item_id"]] = item
    for rec in recs.get("recommendations", []):
        if rec.get("recommendation") != "keep":
            item = _improvement_item(rec["job_id"], rec.get("job_name") or rec["job_id"], "unknown", "timing", "medium", f"Schedule advisor recommends {rec['recommendation']}", [str(rec_path)], rec.get("rationale") or "Review cadence", "cron_maintainer")
            items[item["item_id"]] = item
    payload = {"schema": "hermes.cron-agent-review-inbox.v1", "updated_at": _iso_now(), "items": sorted(items.values(), key=lambda x: (x["severity"], x["item_id"]))}
    atomic_write_json(base_dir() / "agent-review-inbox.json", payload)
    return payload


def _top(items: Iterable[dict[str, Any]], key, limit: int = 5) -> list[dict[str, Any]]:
    return list(sorted(items, key=key, reverse=True))[:limit]


def _candidate_row_quality(row: dict[str, Any]) -> str:
    learning = str(row.get("learning_value") or "").lower()
    if row.get("proof_status") == "FAIL" and row.get("status") == "success":
        return "golden"
    if row.get("failure_class") in {"malformed_state", "validation_failed", "runtime_unhealthy"}:
        return "golden"
    if "handoff" in learning or "permission" in learning or "schedule" in learning:
        return "silver"
    if row.get("status") not in {None, "success"} and row.get("failure_class") not in {None, "none", "unknown"}:
        return "silver"
    return "junk"


def _candidate_quality_counts() -> tuple[dict[str, int], list[dict[str, Any]], list[dict[str, Any]]]:
    counts = {"golden_count": 0, "silver_count": 0, "junk_count": 0, "unlabeled_count": 0, "unique_candidate_count": 0, "duplicate_candidate_count": 0}
    seen: set[str] = set()
    golden: list[dict[str, Any]] = []
    junk: list[dict[str, Any]] = []
    for row in _read_jsonl(base_dir() / "qwen-training-candidates.jsonl"):
        key = str(row.get("candidate_key") or row.get("run_id") or json.dumps(row, sort_keys=True))
        if key in seen:
            counts["duplicate_candidate_count"] += 1
            continue
        seen.add(key)
        counts["unique_candidate_count"] += 1
        label = row.get("quality_label") or _candidate_row_quality(row)
        if label == "golden":
            counts["golden_count"] += 1
            golden.append(row)
        elif label == "silver":
            counts["silver_count"] += 1
        elif label == "junk":
            counts["junk_count"] += 1
            junk.append(row)
        else:
            counts["unlabeled_count"] += 1
    return counts, golden[:5], junk[:5]


def generate_agent_review_rollup() -> dict[str, Any]:
    """Produce the first-pass operator leverage view from the cron ledger."""
    summary_path = base_dir() / "job-health-summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else summarize_cron_observability()
    rec_path = base_dir() / "schedule-recommendations.json"
    recs = json.loads(rec_path.read_text(encoding="utf-8")) if rec_path.exists() else generate_schedule_recommendations()
    handoff_path = base_dir() / "handoff-health.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8")) if handoff_path.exists() else {"stale_handoffs": []}

    jobs = []
    for job_id, health in summary.get("jobs", {}).items():
        jobs.append({
            "job_id": job_id,
            "job_name": health.get("job_name") or job_id,
            "run_count": health.get("run_count", 0),
            "action_rate": health.get("action_rate", 0),
            "action_count": round((health.get("action_rate", 0) or 0) * (health.get("run_count", 0) or 0), 3),
            "no_action_rate": health.get("no_action_rate", 0),
            "duplicate_noise_rate": health.get("duplicate_noise_rate", 0),
            "consecutive_failures": health.get("consecutive_failures", 0),
            "success_rate_7d": health.get("success_rate_7d", 0),
            "stale_handoff_count": health.get("stale_handoff_count", 0),
            "recommendation": health.get("recommendation"),
        })
    rec_by_job = {r.get("job_id"): r for r in recs.get("recommendations", [])}
    cadence = [
        {**j, "advisor_recommendation": rec_by_job.get(j["job_id"], {}).get("recommendation")}
        for j in jobs
        if rec_by_job.get(j["job_id"], {}).get("recommendation") not in {None, "keep"}
        or j.get("recommendation") not in {None, "keep schedule"}
    ]
    qwen_counts, golden, junk = _candidate_quality_counts()
    payload = {
        "schema": "hermes.cron-agent-review-rollup.v1",
        "updated_at": _iso_now(),
        "highest_value_jobs": _top([j for j in jobs if j["action_count"] > 0], key=lambda j: (j["action_count"], j["action_rate"], -j["consecutive_failures"])),
        "noisiest_jobs": _top(jobs, key=lambda j: (j["no_action_rate"] + j["duplicate_noise_rate"], j["stale_handoff_count"])),
        "failing_or_flapping_jobs": _top([j for j in jobs if j["consecutive_failures"] or j["success_rate_7d"] < 1], key=lambda j: (j["consecutive_failures"], 1 - j["success_rate_7d"])),
        "cadence_change_candidates": _top(cadence, key=lambda j: (j["consecutive_failures"], j["no_action_rate"], j["stale_handoff_count"])),
        "unconsumed_handoffs": list(handoff.get("stale_handoffs", []))[:25],
        "qwen_quality": qwen_counts,
        "golden_qwen_examples": golden,
        "junk_qwen_examples": junk,
        "notes": "Quality labels are triage labels only; examples remain unreviewed and unapproved for training until a human/agent review marks them otherwise.",
    }
    atomic_write_json(base_dir() / "agent-review-rollup.json", payload)
    atomic_write_json(base_dir() / "qwen-training-quality-summary.json", {"updated_at": payload["updated_at"], **qwen_counts, "golden_examples": golden, "junk_examples": junk})
    return payload


def _schemas() -> dict[str, dict[str, Any]]:
    return {
        "cron-run-record-v1.json": {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": SCHEMA_RUN, "type": "object", "required": sorted(_REQUIRED_RUN_FIELDS), "properties": {"schema": {"const": SCHEMA_RUN}, "status": {"enum": ["success", "failed", "failed_closed", "partial", "skipped", "unknown"]}, "proof_status": {"enum": ["PASS", "FAIL", "N/A", "unknown"]}, "noise_score": {"enum": ["LOW", "MEDIUM", "HIGH"]}, "usefulness_score": {"enum": ["LOW", "MEDIUM", "HIGH"]}}},
        "cron-job-health-v1.json": {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": SCHEMA_HEALTH, "type": "object", "required": ["schema", "updated_at", "jobs"]},
        "cron-improvement-item-v1.json": {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": SCHEMA_IMPROVEMENT, "type": "object", "required": ["schema", "item_id", "created_at", "job_id", "job_name", "org", "issue_type", "severity", "summary", "evidence_paths", "suggested_fix", "owner", "status", "reviewed_by", "resolution"]},
    }


def _helper_script(kind: str) -> str:
    module = "cron.observability"
    if kind == "capture":
        body = "capture_cli()"
    elif kind == "summarize":
        body = "summarize_cli()"
    elif kind == "advisor":
        body = "advisor_cli()"
    elif kind == "inbox":
        body = "inbox_cli()"
    elif kind == "collector":
        body = "collector_cli()"
    else:
        raise ValueError(kind)
    return f"#!/usr/bin/env python3\nfrom {module} import {body.split('(')[0]}\nif __name__ == '__main__':\n    {body}\n"


def ensure_observability_install() -> dict[str, Path]:
    b = base_dir()
    for rel in ["schemas", "runs"]:
        p = b / rel
        p.mkdir(parents=True, exist_ok=True)
        _secure_dir(p)
    for name, schema in _schemas().items():
        atomic_write_json(b / "schemas" / name, schema)
    scripts = {
        "capture_cron_run.py": "capture",
        "summarize_cron_observability.py": "summarize",
        "cron_schedule_advisor.py": "advisor",
        "cron_agent_review_inbox.py": "inbox",
        "cron_observability_collector.py": "collector",
    }
    for fname, kind in scripts.items():
        p = b / fname
        atomic_write_text(p, _helper_script(kind))
        try:
            os.chmod(p, 0o700)
        except Exception:
            pass
    # Seed durable files if absent without erasing existing ledgers.
    defaults = {
        "job-health-summary.json": {"schema": SCHEMA_HEALTH, "updated_at": _iso_now(), "jobs": {}},
        "agent-review-inbox.json": {"schema": "hermes.cron-agent-review-inbox.v1", "updated_at": _iso_now(), "items": []},
        "schedule-recommendations.json": {"updated_at": _iso_now(), "recommendations": [], "applied_changes": []},
        "handoff-health.json": {"schema": "hermes.cron-handoff-health.v1", "updated_at": _iso_now(), "stale_handoffs": [], "by_job": {}},
        "agent-review-rollup.json": {"schema": "hermes.cron-agent-review-rollup.v1", "updated_at": _iso_now(), "highest_value_jobs": [], "noisiest_jobs": [], "failing_or_flapping_jobs": [], "cadence_change_candidates": [], "unconsumed_handoffs": [], "qwen_quality": {}},
        "qwen-training-quality-summary.json": {"updated_at": _iso_now(), "golden_count": 0, "silver_count": 0, "junk_count": 0, "unlabeled_count": 0, "unique_candidate_count": 0, "duplicate_candidate_count": 0, "golden_examples": [], "junk_examples": []},
    }
    for fname, payload in defaults.items():
        p = b / fname
        if not p.exists():
            atomic_write_json(p, payload)
    for fname in ["cron-run-ledger.jsonl", "qwen-training-candidates.jsonl", "redaction-log.jsonl"]:
        p = b / fname
        if not p.exists():
            atomic_write_text(p, "")
    return {
        "base_dir": b,
        "run_record_schema": b / "schemas" / "cron-run-record-v1.json",
        "job_health_schema": b / "schemas" / "cron-job-health-v1.json",
        "improvement_schema": b / "schemas" / "cron-improvement-item-v1.json",
    }


def capture_cli() -> None:
    parser = argparse.ArgumentParser(description="Capture a Hermes cron run observability record")
    parser.add_argument("--job-json", required=True)
    parser.add_argument("--scheduled-at")
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--finished-at", required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--output-file")
    parser.add_argument("--output")
    parser.add_argument("--cron-output-path")
    args = parser.parse_args()
    job = json.loads(Path(args.job_json).read_text(encoding="utf-8")) if Path(args.job_json).exists() else json.loads(args.job_json)
    output = args.output or (Path(args.output_file).read_text(encoding="utf-8", errors="replace") if args.output_file else "")
    record = capture_cron_run(job=job, scheduled_at=args.scheduled_at, started_at=args.started_at, finished_at=args.finished_at, exit_code=args.exit_code, output=output, cron_output_path=args.cron_output_path or args.output_file)
    print(json.dumps({"captured": record["run_id"], "should_agent_review": record["should_agent_review"]}))


def summarize_cli() -> None:
    print(json.dumps(summarize_cron_observability(), indent=2))


def advisor_cli() -> None:
    print(json.dumps(generate_schedule_recommendations(), indent=2))


def inbox_cli() -> None:
    print(json.dumps(generate_agent_review_inbox(), indent=2))


def collector_cli() -> None:
    print(json.dumps(collect_existing_cron_outputs(), indent=2))


if __name__ == "__main__":
    collector_cli()
