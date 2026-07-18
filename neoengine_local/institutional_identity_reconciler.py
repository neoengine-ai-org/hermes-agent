"""Per-profile institutional-identity reconciliation for cron stores.

"Institutional identity" is a NeoEngine-specific concept (which institutional
identity a scheduled job acts on behalf of), NOT generic upstream Hermes
behaviour — so this tooling lives under ``neoengine_local/`` and never
hardcodes NeoEngine policy into the upstream ``cron/`` core.

Contract:

- Operates on EXACTLY ONE profile's cron store per run (a single
  ``cron/jobs.json``). It never iterates ``profiles/`` and never touches more
  than the one store path it was pinned to (issue #4707 per-profile
  isolation).
- DRY-RUN by default. A mutation mode exists (``--execute``) but refuses to
  run unless it is handed a CONCRETE ``jobs.json`` path — a bare HERMES_HOME
  (implied default store) is never mutated.
- Reads the profile's ENABLED jobs and maps each to its institutional-identity
  id via an explicit, caller-provided mapping (job-id / command / tag ->
  identity id). The mapping is data, not code.
- Fail-closed refusals, each a distinct classification (see ``Finding``):
  inactive/paused profile is skipped; ambiguous mappings are refused;
  a job already bound to a different identity than the mapping says is refused
  (never silently overwritten); a null identity on an enabled job is reported
  as needing attention and, in mutation mode ONLY, bound when the mapping is
  unambiguous and the job is currently null.
- IDEMPOTENT: a second run after a successful bind produces zero changes.
- REDACTS SECRETS: token/key/password-shaped values are scrubbed from every
  emitted command/env string.
- Emits a FIELD-LEVEL INVERSE PATCH (per-job, per-field old->new deltas) so a
  rollback is a precise inverse rather than a blind whole-file restore, and
  reports job additions/removals detected between a prior snapshot and now.
- A Qwen preflight error classifier surfaces enabled jobs whose last
  preflight/status is an error (see ``qwen35_lane_experience``) as
  ``QWEN_PREFLIGHT_ERROR`` and fails them closed (never mutated).

Kill switch:

- ``INSTITUTIONAL_IDENTITY_RECONCILER_DISABLED=1`` disables everything; the
  run mutates nothing and reports the disable.
"""

from __future__ import annotations

import argparse
import enum
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # canonical home resolution; fallback for standalone invocation
    from hermes_constants import get_hermes_home as _get_hermes_home
except ImportError:  # pragma: no cover - standalone
    def _get_hermes_home() -> Path:
        return Path.home() / ".hermes"

# Reuse the Qwen preflight notion of a failed terminal status if available,
# otherwise fall back to accepting a status field on the job. Import is
# defensive so this module never hard-depends on the qwen lane module.
try:  # pragma: no cover - trivial import guard
    from neoengine_local.qwen35_lane_experience import (
        VALID_TERMINAL_STATUSES as _QWEN_TERMINAL_STATUSES,
    )
except Exception:  # pragma: no cover
    _QWEN_TERMINAL_STATUSES = {
        "PRODUCTIVE_DIFF_WITH_EVIDENCE",
        "NO_CHANGE_WITH_EVIDENCE",
        "FAILED_TOOLING_OR_CONTEXT",
    }

TOOL_NAME = "institutional-identity-reconciler"
TOOL_VERSION = "1.0.0"

# The per-job field that carries the institutional identity binding. Cron jobs
# currently have no such field, so absent == null == unbound.
DEFAULT_IDENTITY_FIELD = "institutional_identity"

# Job fields consulted for a Qwen preflight / last status verdict. A value that
# looks like an error (contains "error"/"fail") OR equals a qwen terminal
# status that denotes failure classifies the job as a preflight error.
_STATUS_FIELDS = (
    "preflight_status",
    "last_preflight_status",
    "qwen_preflight_status",
    "last_status",
    "status",
)
_QWEN_ERROR_STATUSES = {
    s for s in _QWEN_TERMINAL_STATUSES if "FAIL" in s.upper() or "ERROR" in s.upper()
}


class Finding(str, enum.Enum):
    """Distinct, testable per-job classifications."""

    SKIPPED_INACTIVE_PROFILE = "SKIPPED_INACTIVE_PROFILE"
    QWEN_PREFLIGHT_ERROR = "QWEN_PREFLIGHT_ERROR"
    AMBIGUOUS_MAPPING = "AMBIGUOUS_MAPPING"
    WRONG_IDENTITY_CONFLICT = "WRONG_IDENTITY_CONFLICT"
    NULL_IDENTITY_NEEDS_BIND = "NULL_IDENTITY_NEEDS_BIND"
    BOUND = "BOUND"
    ALREADY_CORRECT = "ALREADY_CORRECT"
    NO_MAPPING = "NO_MAPPING"


# Top-level refusal reasons (whole-run, not per-job).
class Refusal(str, enum.Enum):
    EXECUTE_WITHOUT_EXPLICIT_JOBS_FILE = "EXECUTE_WITHOUT_EXPLICIT_JOBS_FILE"
    CROSS_PROFILE = "CROSS_PROFILE"
    DISABLED = "DISABLED"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

_REDACTED = "***REDACTED***"

# Order matters: the key=value form runs before the opaque-token catch-all so
# the key label is preserved while the value is scrubbed.
_SECRET_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # PEM / private key blocks
    (re.compile(r"-----BEGIN[^-]*-----.*?-----END[^-]*-----", re.DOTALL), _REDACTED),
    (re.compile(r"-----BEGIN[^-]*-----"), _REDACTED),
    # AWS access key ids
    (re.compile(r"\bAKIA[0-9A-Z]{12,}\b"), _REDACTED),
    # Bearer / Basic auth tokens
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/\-]+=*"), r"\1 " + _REDACTED),
    # key=value / key: value where the key looks credential-shaped
    (
        re.compile(
            r"(?i)(-{0,2}(?:api[_-]?key|secret|password|passwd|token|"
            r"access[_-]?key|secret[_-]?key|private[_-]?key|client[_-]?secret|"
            r"auth[_-]?token|credential)[\"']?\s*[:=]\s*)"
            r"([^\s\"'&;|]+)"
        ),
        r"\1" + _REDACTED,
    ),
    # Long opaque high-entropy-ish tokens (>=32 of the token alphabet)
    (re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"), _REDACTED),
]


def redact_secrets(text: Optional[str]) -> Optional[str]:
    """Return ``text`` with secret-looking values replaced by a placeholder.

    Pattern-based only — no allow-list of specific secrets — so an unknown
    token still gets scrubbed. Returns ``None`` unchanged.
    """
    if text is None:
        return None
    scrubbed = str(text)
    for pattern, repl in _SECRET_PATTERNS:
        scrubbed = pattern.sub(repl, scrubbed)
    return scrubbed


def _job_command_text(job: Dict[str, Any]) -> Optional[str]:
    """Best-effort human-readable command/env text for a job (pre-redaction)."""
    parts: List[str] = []
    for key in ("command", "script", "prompt"):
        value = job.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    env = job.get("env")
    if isinstance(env, dict):
        parts.extend(f"{k}={v}" for k, v in env.items())
    if not parts:
        return None
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Config + report objects
# ---------------------------------------------------------------------------

@dataclass
class ReconcileConfig:
    """Everything a single reconcile run is pinned to.

    Exactly one of ``jobs_file`` (a concrete jobs.json) or ``home`` (a
    HERMES_HOME whose default-profile store is derived) resolves the single
    store path. ``jobs_file_explicit`` records whether the path was given as a
    concrete file — mutation is only ever permitted for an explicit file.
    """

    jobs_file: Path
    jobs_file_explicit: bool
    mapping: Dict[str, Any]
    execute: bool = False
    identity_field: str = DEFAULT_IDENTITY_FIELD
    profile_active_override: Optional[bool] = None
    snapshot_jobs: Optional[List[Dict[str, Any]]] = None


@dataclass
class JobFinding:
    job_id: str
    finding: Finding
    current_identity: Optional[str] = None
    target_identity: Optional[str] = None
    needs_attention: bool = False
    redacted_command: Optional[str] = None
    note: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "finding": self.finding.value,
            "current_identity": self.current_identity,
            "target_identity": self.target_identity,
            "needs_attention": self.needs_attention,
            "redacted_command": self.redacted_command,
            "note": self.note,
        }


@dataclass
class ReconcileReport:
    jobs_file: str
    execute: bool
    profile_active: bool = True
    disabled: bool = False
    refused: bool = False
    refusal_reason: Optional[str] = None
    mutated: bool = False
    entries: List[JobFinding] = field(default_factory=list)
    inverse_patch: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": TOOL_NAME,
            "tool_version": TOOL_VERSION,
            "jobs_file": self.jobs_file,
            "execute": self.execute,
            "profile_active": self.profile_active,
            "disabled": self.disabled,
            "refused": self.refused,
            "refusal_reason": self.refusal_reason,
            "mutated": self.mutated,
            "entries": [e.to_dict() for e in self.entries],
            "inverse_patch": self.inverse_patch,
            "notes": self.notes,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Store IO
# ---------------------------------------------------------------------------

def _load_store(jobs_file: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Load the raw store dict and the jobs list. Missing file => empty."""
    if not jobs_file.exists():
        return {}, []
    with open(jobs_file, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return {"jobs": data}, [j for j in data if isinstance(j, dict)]
    if isinstance(data, dict):
        jobs = data.get("jobs", [])
        if not isinstance(jobs, list):
            jobs = []
        return data, [j for j in jobs if isinstance(j, dict)]
    raise ValueError(f"unexpected jobs.json shape: {type(data).__name__}")


def _profile_active(data: Dict[str, Any], override: Optional[bool]) -> bool:
    if override is not None:
        return override
    if data.get("paused") is True:
        return False
    if data.get("active") is False:
        return False
    if data.get("profile_active") is False:
        return False
    return True


def _atomic_write_store(jobs_file: Path, data: Dict[str, Any]) -> None:
    jobs_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(jobs_file.parent), prefix=".iir_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, jobs_file)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def _is_enabled(job: Dict[str, Any]) -> bool:
    return bool(job.get("enabled", True))


def _job_id(job: Dict[str, Any]) -> str:
    return str(job.get("id") or job.get("job_id") or "").strip()


def _current_identity(job: Dict[str, Any], identity_field: str) -> Optional[str]:
    value = job.get(identity_field)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def is_qwen_preflight_error(job: Dict[str, Any]) -> bool:
    """True when the job's last preflight/status denotes an error.

    Reuses the Qwen preflight terminal-status vocabulary and also accepts a
    plain ``error``/``failed`` status field for jobs that carry one.
    """
    for key in _STATUS_FIELDS:
        raw = job.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        upper = text.upper()
        if upper in _QWEN_ERROR_STATUSES:
            return True
        if "ERROR" in upper or "FAIL" in upper:
            return True
    return False


def resolve_candidate_identities(
    job: Dict[str, Any], mapping: Dict[str, Any]
) -> List[str]:
    """Return the DISTINCT candidate identity ids a job maps to.

    Mapping shape (all optional)::

        {"by_id": {job_id: identity}, "by_command": {cmd: identity},
         "by_tag": {tag: identity}}

    A job matching selectors that disagree yields >1 candidate == ambiguous.
    """
    candidates: List[str] = []

    def _add(value: Any) -> None:
        if value is None:
            return
        text = str(value).strip()
        if text and text not in candidates:
            candidates.append(text)

    by_id = mapping.get("by_id") or {}
    if isinstance(by_id, dict):
        _add(by_id.get(_job_id(job)))

    by_command = mapping.get("by_command") or {}
    if isinstance(by_command, dict):
        for key in ("command", "script"):
            cmd = job.get(key)
            if isinstance(cmd, str) and cmd.strip():
                _add(by_command.get(cmd.strip()))

    by_tag = mapping.get("by_tag") or {}
    if isinstance(by_tag, dict):
        tags = job.get("tags")
        if isinstance(tags, (list, tuple)):
            for tag in tags:
                _add(by_tag.get(str(tag).strip()))

    return candidates


def _classify_job(
    job: Dict[str, Any], cfg: ReconcileConfig
) -> Tuple[JobFinding, Optional[str]]:
    """Classify one enabled job. Returns (finding, target_to_bind).

    ``target_to_bind`` is non-None ONLY when a mutation-mode bind is
    warranted (null identity + single unambiguous target + no preflight error).
    """
    job_id = _job_id(job) or "<unknown>"
    current = _current_identity(job, cfg.identity_field)
    redacted = redact_secrets(_job_command_text(job))

    # Fail closed on Qwen preflight errors before any binding logic.
    if is_qwen_preflight_error(job):
        return (
            JobFinding(
                job_id=job_id,
                finding=Finding.QWEN_PREFLIGHT_ERROR,
                current_identity=current,
                needs_attention=True,
                redacted_command=redacted,
                note="job last preflight/status is an error — surfaced, not mutated",
            ),
            None,
        )

    candidates = resolve_candidate_identities(job, cfg.mapping)

    if len(candidates) > 1:
        return (
            JobFinding(
                job_id=job_id,
                finding=Finding.AMBIGUOUS_MAPPING,
                current_identity=current,
                needs_attention=True,
                redacted_command=redacted,
                note=f"job maps to {len(candidates)} candidate identities",
            ),
            None,
        )

    if not candidates:
        return (
            JobFinding(
                job_id=job_id,
                finding=Finding.NO_MAPPING,
                current_identity=current,
                needs_attention=current is None,
                redacted_command=redacted,
                note="no mapping entry for this job",
            ),
            None,
        )

    target = candidates[0]

    if current is None:
        return (
            JobFinding(
                job_id=job_id,
                finding=Finding.NULL_IDENTITY_NEEDS_BIND,
                current_identity=None,
                target_identity=target,
                needs_attention=True,
                redacted_command=redacted,
                note="enabled job has no institutional identity",
            ),
            target,
        )

    if current == target:
        return (
            JobFinding(
                job_id=job_id,
                finding=Finding.ALREADY_CORRECT,
                current_identity=current,
                target_identity=target,
                redacted_command=redacted,
            ),
            None,
        )

    # current != target — never silently overwrite.
    return (
        JobFinding(
            job_id=job_id,
            finding=Finding.WRONG_IDENTITY_CONFLICT,
            current_identity=current,
            target_identity=target,
            needs_attention=True,
            redacted_command=redacted,
            note="job bound to a different identity than the mapping says",
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def reconcile(cfg: ReconcileConfig) -> ReconcileReport:
    """Run reconciliation for a single profile store. Never raises."""
    report = ReconcileReport(jobs_file=str(cfg.jobs_file), execute=cfg.execute)

    if _env_flag("INSTITUTIONAL_IDENTITY_RECONCILER_DISABLED"):
        report.disabled = True
        report.refused = True
        report.refusal_reason = Refusal.DISABLED.value
        report.notes.append("INSTITUTIONAL_IDENTITY_RECONCILER_DISABLED is set")
        return report

    # Mutation refusal: a bare HERMES_HOME (implied default store) is never
    # mutated — execute demands a concrete jobs.json path.
    if cfg.execute and not cfg.jobs_file_explicit:
        report.refused = True
        report.refusal_reason = Refusal.EXECUTE_WITHOUT_EXPLICIT_JOBS_FILE.value
        report.notes.append(
            "refusing to mutate: --execute requires an explicit --jobs-file "
            "(a concrete jobs.json), not an implied HERMES_HOME default store"
        )
        return report

    try:
        data, jobs = _load_store(cfg.jobs_file)
    except Exception as exc:  # never-raise contract
        report.errors.append(f"failed to load store: {exc}")
        return report

    report.profile_active = _profile_active(data, cfg.profile_active_override)

    # Snapshot delta (intervening additions/removals) — computed regardless of
    # mutation so a dry-run surfaces drift too.
    report.inverse_patch = _build_inverse_patch(cfg, jobs, field_deltas=[])

    if not report.profile_active:
        report.notes.append("profile is inactive/paused — skipping (no mutation)")
        for job in jobs:
            if not _is_enabled(job):
                continue
            report.entries.append(
                JobFinding(
                    job_id=_job_id(job) or "<unknown>",
                    finding=Finding.SKIPPED_INACTIVE_PROFILE,
                    current_identity=_current_identity(job, cfg.identity_field),
                    redacted_command=redact_secrets(_job_command_text(job)),
                    note="inactive/paused profile",
                )
            )
        return report

    field_deltas: List[Dict[str, Any]] = []
    bound_any = False

    for job in jobs:
        if not _is_enabled(job):
            continue
        try:
            finding, target = _classify_job(job, cfg)
        except Exception as exc:  # never-raise per-job
            report.errors.append(f"classify {_job_id(job)!r}: {exc}")
            continue

        if cfg.execute and target is not None and finding.finding == Finding.NULL_IDENTITY_NEEDS_BIND:
            old = job.get(cfg.identity_field)
            job[cfg.identity_field] = target
            field_deltas.append(
                {
                    "job_id": finding.job_id,
                    "field": cfg.identity_field,
                    "old": old,
                    "new": target,
                }
            )
            finding.finding = Finding.BOUND
            finding.current_identity = target
            finding.needs_attention = False
            finding.note = "bound null identity to mapping target"
            bound_any = True

        report.entries.append(finding)

    report.inverse_patch = _build_inverse_patch(cfg, jobs, field_deltas)

    if cfg.execute and bound_any:
        try:
            data["jobs"] = jobs
            data["updated_at"] = _utcnow_iso()
            _atomic_write_store(cfg.jobs_file, data)
            report.mutated = True
        except Exception as exc:  # never-raise
            report.errors.append(f"failed to write store: {exc}")
            report.mutated = False

    return report


def _build_inverse_patch(
    cfg: ReconcileConfig,
    current_jobs: List[Dict[str, Any]],
    field_deltas: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Field-level inverse patch + snapshot drift.

    ``field_deltas`` records old->new for each field this run changed, so
    rollback is the precise inverse (set ``field`` back to ``old``). Snapshot
    drift reports jobs added/removed between a prior snapshot and now — the
    tool reports them, it never blindly restores or clobbers them.
    """
    patch: Dict[str, Any] = {
        "jobs_file": str(cfg.jobs_file),
        "identity_field": cfg.identity_field,
        "field_deltas": field_deltas,
        "added_jobs": [],
        "removed_jobs": [],
    }
    if cfg.snapshot_jobs is None:
        return patch
    snapshot_ids = {
        str(j.get("id") or j.get("job_id") or "").strip()
        for j in cfg.snapshot_jobs
        if isinstance(j, dict)
    }
    snapshot_ids.discard("")
    current_ids = {_job_id(j) for j in current_jobs}
    current_ids.discard("")
    patch["added_jobs"] = sorted(current_ids - snapshot_ids)
    patch["removed_jobs"] = sorted(snapshot_ids - current_ids)
    return patch


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve_jobs_file(
    jobs_file_arg: Optional[str], home_arg: Optional[str]
) -> Tuple[Path, bool]:
    """Resolve the single store path and whether it was given explicitly.

    Precedence: an explicit ``--jobs-file`` (concrete path, explicit=True);
    else the default-profile store under ``--home`` or the canonical home
    (implied default, explicit=False). Only an explicit path may be mutated.
    """
    if jobs_file_arg:
        return Path(jobs_file_arg).expanduser(), True
    home = Path(home_arg).expanduser() if home_arg else _get_hermes_home()
    return home / "cron" / "jobs.json", False


def _load_json_file(path: Optional[str]) -> Any:
    if not path:
        return None
    with open(Path(path).expanduser(), "r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog=TOOL_NAME, description=__doc__)
    parser.add_argument(
        "--jobs-file",
        default=None,
        help="concrete path to a single profile's cron/jobs.json (required "
        "for --execute)",
    )
    parser.add_argument(
        "--home",
        default=None,
        help="HERMES_HOME whose default-profile store is used (dry-run only; "
        "never mutated)",
    )
    parser.add_argument(
        "--mapping",
        default=None,
        help="path to a JSON mapping file (by_id/by_command/by_tag -> identity)",
    )
    parser.add_argument(
        "--snapshot",
        default=None,
        help="path to a prior jobs snapshot (list of jobs or a jobs.json) for "
        "add/remove drift reporting",
    )
    parser.add_argument(
        "--identity-field",
        default=DEFAULT_IDENTITY_FIELD,
        help=f"job field holding the identity (default: {DEFAULT_IDENTITY_FIELD})",
    )
    parser.add_argument(
        "--profile-inactive",
        action="store_true",
        help="force-treat the profile as inactive/paused (skip, no mutation)",
    )
    parser.add_argument("--execute", action="store_true", help="mutate (bind) — "
                        "requires an explicit --jobs-file")
    args = parser.parse_args(argv)

    jobs_file, explicit = _resolve_jobs_file(args.jobs_file, args.home)

    try:
        mapping = _load_json_file(args.mapping) or {}
    except Exception as exc:
        print(f"{TOOL_NAME}: failed to read --mapping: {exc}", file=sys.stderr)
        return 2
    if not isinstance(mapping, dict):
        print(f"{TOOL_NAME}: --mapping must be a JSON object", file=sys.stderr)
        return 2

    snapshot_jobs: Optional[List[Dict[str, Any]]] = None
    try:
        snap = _load_json_file(args.snapshot)
    except Exception as exc:
        print(f"{TOOL_NAME}: failed to read --snapshot: {exc}", file=sys.stderr)
        return 2
    if isinstance(snap, dict):
        snapshot_jobs = snap.get("jobs") if isinstance(snap.get("jobs"), list) else []
    elif isinstance(snap, list):
        snapshot_jobs = snap

    cfg = ReconcileConfig(
        jobs_file=jobs_file,
        jobs_file_explicit=explicit,
        mapping=mapping,
        execute=bool(args.execute),
        identity_field=args.identity_field,
        profile_active_override=False if args.profile_inactive else None,
        snapshot_jobs=snapshot_jobs,
    )

    report = reconcile(cfg)
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))

    if report.refused or report.errors:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
