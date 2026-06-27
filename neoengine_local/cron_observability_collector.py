from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_SAMPLE_BYTES = 4096
DEFAULT_MAX_SCAN_FILES = 500
DEFAULT_BUDGET_SECONDS = 20.0

SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s`]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s`]+"),
)


@dataclass(frozen=True)
class CollectorPaths:
    hermes_home: Path
    cron_output: Path
    state_dir: Path
    captured_path: Path
    ledger_path: Path
    summary_path: Path

    @classmethod
    def from_home(cls, hermes_home: str | os.PathLike[str]) -> "CollectorPaths":
        home = Path(hermes_home)
        state_dir = home / "state" / "cron-observability"
        return cls(
            hermes_home=home,
            cron_output=home / "cron" / "output",
            state_dir=state_dir,
            captured_path=state_dir / "captured-files.json",
            ledger_path=state_dir / "run-ledger.jsonl",
            summary_path=state_dir / "collector-summary.json",
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def redact_cron_sample(text: str) -> tuple[str, bool]:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(lambda match: match.group(1) + "[REDACTED]", redacted)
    return redacted, redacted != text


def file_digest_prefix(path: Path, *, sample_bytes: int = MAX_SAMPLE_BYTES) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        hasher.update(handle.read(sample_bytes))
    stat = path.stat()
    return f"prefix{sample_bytes}:{hasher.hexdigest()}:{stat.st_size}:{int(stat.st_mtime)}"


def _sorted_job_dirs(cron_output: Path) -> list[Path]:
    job_dirs: list[tuple[float, Path]] = []
    try:
        with os.scandir(cron_output) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    job_dirs.append((entry.stat(follow_symlinks=False).st_mtime, Path(entry.path)))
    except OSError:
        return []
    return [path for _, path in sorted(job_dirs, reverse=True)]


def iter_receipts(cron_output: Path, *, max_scan_files: int, deadline: float) -> tuple[list[Path], bool]:
    if not cron_output.exists():
        return [], False

    paths: list[Path] = []
    budget_exhausted = False
    for job_dir in _sorted_job_dirs(cron_output):
        if len(paths) >= max_scan_files or time.monotonic() > deadline:
            budget_exhausted = True
            break
        for root, dirs, files in os.walk(job_dir):
            if len(paths) >= max_scan_files or time.monotonic() > deadline:
                budget_exhausted = True
                break
            dirs.sort(reverse=True)
            for name in sorted(files, reverse=True):
                if name.startswith("."):
                    continue
                paths.append(Path(root) / name)
                if len(paths) >= max_scan_files or time.monotonic() > deadline:
                    budget_exhausted = True
                    break
            if budget_exhausted:
                break
    return paths, budget_exhausted


def collect_existing_cron_outputs(
    *,
    hermes_home: str | os.PathLike[str] | None = None,
    max_new: int = 2000,
    max_scan_files: int = DEFAULT_MAX_SCAN_FILES,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    now: str | None = None,
) -> dict[str, Any]:
    home = Path(hermes_home or os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    paths = CollectorPaths.from_home(home)
    deadline = time.monotonic() + budget_seconds
    paths.state_dir.mkdir(parents=True, exist_ok=True)

    captured: dict[str, Any] = load_json(paths.captured_path, {})
    checked_at = now or utc_now()
    new_records: list[dict[str, Any]] = []
    redactions = 0
    scanned = 0
    receipt_paths, budget_exhausted = iter_receipts(paths.cron_output, max_scan_files=max_scan_files, deadline=deadline)

    for path in receipt_paths:
        if len(new_records) >= max_new or time.monotonic() > deadline:
            budget_exhausted = True
            break
        key = str(path)
        try:
            stat = path.stat()
            digest = file_digest_prefix(path)
        except OSError:
            continue
        scanned += 1
        prior = captured.get(key)
        if prior and prior.get("digest") == digest:
            continue

        raw = path.read_text(errors="replace")[:MAX_SAMPLE_BYTES]
        sample, was_redacted = redact_cron_sample(raw)
        redactions += int(was_redacted)
        relative_parts = path.relative_to(paths.cron_output).parts if path.is_relative_to(paths.cron_output) else ()
        record = {
            "captured_at": checked_at,
            "path": key,
            "job_id": relative_parts[0] if relative_parts else None,
            "size_bytes": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "digest": digest,
            "sample": sample,
            "sample_truncated": stat.st_size > MAX_SAMPLE_BYTES,
        }
        new_records.append(record)
        captured[key] = {"digest": digest, "captured_at": checked_at, "size_bytes": stat.st_size}

    if new_records:
        with paths.ledger_path.open("a") as ledger:
            for record in new_records:
                ledger.write(json.dumps(record, sort_keys=True) + "\n")
        paths.captured_path.write_text(json.dumps(captured, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    summary = {
        "status": "PASS",
        "checked_at": checked_at,
        "source": str(paths.cron_output),
        "captured": len(new_records),
        "known_files": len(captured),
        "scanned_files": scanned,
        "budget_exhausted": budget_exhausted,
        "redacted_records": redactions,
        "ledger_path": str(paths.ledger_path),
        "summary_path": str(paths.summary_path),
        "mode": "script_only_self_contained_budgeted_collector",
    }
    paths.summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def collector_main() -> int:
    max_new = int(os.getenv("HERMES_CRON_OBSERVABILITY_COLLECTOR_MAX_NEW", "2000"))
    max_scan_files = int(os.getenv("HERMES_CRON_OBSERVABILITY_COLLECTOR_MAX_SCAN", str(DEFAULT_MAX_SCAN_FILES)))
    budget_seconds = float(os.getenv("HERMES_CRON_OBSERVABILITY_COLLECTOR_BUDGET_SECONDS", str(DEFAULT_BUDGET_SECONDS)))
    summary = collect_existing_cron_outputs(
        max_new=max_new,
        max_scan_files=max_scan_files,
        budget_seconds=budget_seconds,
    )
    if summary.get("captured"):
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(collector_main())
