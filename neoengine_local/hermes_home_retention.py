"""Hermes home retention & rotation.

Bounded, allowlist-only retention for runtime data under the hermes home
(default ``~/.hermes``). Five lanes: dispatch logs, orphaned lane workdirs,
cron run output, state.db row retention + VACUUM, and profile caches, with
recursion into ``profiles/<name>/`` homes for the shapes they mirror.

Contract (see docs/runbooks/hermes-home-retention.md):

- Dry-run by default. ``--execute`` mutates; ``--check`` mutates nothing and
  exits 1 when any lane is over threshold.
- Archive-before-delete: every deleted receipt-class or bulk-class file is
  written to a tar.gz under ``<root>/retention-archive/`` with a sibling
  ``.receipt.json`` first. Receipt-class archives are kept indefinitely;
  bulk-class archives are pruned by this same tool after
  ``--bulk-archive-max-age-days``. Cache-class data (npm/lsp caches) is
  re-derivable and is deleted without archive.
- Allowlist-only targeting. Protected paths (the org-evidence ledger,
  registries, jobs.json, kanban.db, bin/, scripts/, skills/) are never
  candidates because no lane targets them.
- Symlinks are never followed or collected; any target that resolves outside
  the retention root is dropped.
- Lanes never raise; failures are reported per-lane and the run continues.

Kill switches (parsed like the dispatch heartbeats' flags):

- ``HERMES_HOME_RETENTION_DISABLED=1`` disables everything.
- ``HERMES_HOME_RETENTION_<LANE>_DISABLED=1`` disables one lane, LANE in
  ``DISPATCH_LOGS``, ``LANE_WORKDIRS``, ``CRON_OUTPUT``, ``STATE_DB``,
  ``PROFILE_CACHES``, ``PROFILES``, ``ARCHIVE_GC``.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:  # canonical home resolution; fallback for standalone invocation
    from hermes_constants import get_hermes_home as _get_hermes_home
except ImportError:  # pragma: no cover
    def _get_hermes_home() -> Path:
        return Path.home() / ".hermes"

TOOL_NAME = "hermes-home-retention"
TOOL_VERSION = "1.0.0"

ARCHIVE_DIRNAME = "retention-archive"
RUNS_DIRNAME = "state/hermes-home-retention/runs"

CLASS_RECEIPT = "receipt"
CLASS_BULK = "bulk"
CLASS_CACHE = "cache"

RECEIPT_NAME_HINTS = ("receipt",)
RECEIPT_SUFFIXES = {".md"}  # in dispatch log dirs, *.md finals are closeout-ish

DB_DELETE_BATCH = 500


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def _utcnow_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RetentionConfig:
    root: Path
    execute: bool = False
    max_age_days: float = 14.0
    keep_min: int = 25
    workdir_grace_hours: float = 24.0
    db_max_age_days: float = 30.0
    vacuum_threshold_bytes: int = 256 * 1024 * 1024
    bulk_archive_max_age_days: float = 90.0
    warn_files: int = 10_000
    warn_bytes: int = 500 * 1024 * 1024
    db_warn_bytes: int = 2 * 1024 * 1024 * 1024
    runstamp: str = field(default_factory=_utcnow_stamp)

    @property
    def archive_dir(self) -> Path:
        return self.root / ARCHIVE_DIRNAME

    @property
    def age_cutoff(self) -> float:
        return time.time() - self.max_age_days * 86400.0

    @property
    def db_cutoff(self) -> float:
        return time.time() - self.db_max_age_days * 86400.0


@dataclass
class LaneReport:
    lane: str
    enabled: bool = True
    candidate_files: int = 0
    candidate_bytes: int = 0
    acted_files: int = 0
    acted_bytes: int = 0
    archives: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    over_threshold: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lane": self.lane,
            "enabled": self.enabled,
            "candidate_files": self.candidate_files,
            "candidate_bytes": self.candidate_bytes,
            "acted_files": self.acted_files,
            "acted_bytes": self.acted_bytes,
            "archives": self.archives,
            "notes": self.notes,
            "errors": self.errors,
            "over_threshold": self.over_threshold,
        }


def _lane_disabled(lane_env: str) -> bool:
    if _env_flag("HERMES_HOME_RETENTION_DISABLED"):
        return True
    return _env_flag(f"HERMES_HOME_RETENTION_{lane_env}_DISABLED")


def _is_within(root: Path, path: Path) -> bool:
    try:
        resolved_root = root.resolve()
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == resolved_root or str(resolved).startswith(
        str(resolved_root) + os.sep
    )


def _iter_regular_files(directory: Path) -> Iterable[Path]:
    """Yield regular non-symlink files directly inside ``directory``."""
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if entry.is_file():
                yield entry
        except OSError:
            continue


def _file_mtime(path: Path) -> float:
    try:
        return path.lstat().st_mtime
    except OSError:
        return time.time()  # unreadable => treat as fresh (fail-safe)


def _file_size(path: Path) -> int:
    try:
        return path.lstat().st_size
    except OSError:
        return 0


def _split_keep_min(
    files: List[Path], keep_min: int, cutoff: float
) -> List[Path]:
    """Return prune candidates: outside the newest ``keep_min`` AND older
    than ``cutoff``. Ordering ties are stable on path for determinism."""
    ranked = sorted(files, key=lambda p: (_file_mtime(p), str(p)), reverse=True)
    tail = ranked[keep_min:]
    return [p for p in tail if _file_mtime(p) < cutoff]


def _classify(path: Path) -> str:
    name = path.name.lower()
    if any(hint in name for hint in RECEIPT_NAME_HINTS):
        return CLASS_RECEIPT
    if path.suffix.lower() in RECEIPT_SUFFIXES:
        return CLASS_RECEIPT
    return CLASS_BULK


def _slug_for(root: Path, target: Path) -> str:
    try:
        rel = target.resolve().relative_to(root.resolve())
    except ValueError:
        rel = Path(target.name)
    return str(rel).replace(os.sep, "-").replace(".", "")


def _write_archive(
    cfg: RetentionConfig,
    slug: str,
    klass: str,
    files: List[Path],
    extra_members: Optional[List[Tuple[str, bytes]]] = None,
) -> Optional[Path]:
    """Create ``<slug>-<runstamp>.tar.gz`` + sibling receipt. Returns the
    archive path, or None when there is nothing to archive."""
    if not files and not extra_members:
        return None
    cfg.archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = cfg.archive_dir / f"{slug}-{cfg.runstamp}.tar.gz"
    receipt_path = cfg.archive_dir / f"{slug}-{cfg.runstamp}.receipt.json"
    mtimes = [_file_mtime(p) for p in files] or [time.time()]
    byte_count = sum(_file_size(p) for p in files)
    root_resolved = cfg.root.resolve()
    with tarfile.open(archive_path, "w:gz") as tar:
        for path in files:
            try:
                arcname = str(path.resolve().relative_to(root_resolved))
            except ValueError:
                arcname = path.name
            try:
                tar.add(path, arcname=arcname, recursive=False)
            except OSError:
                continue
        for member_name, payload in extra_members or []:
            info = tarfile.TarInfo(name=member_name)
            info.size = len(payload)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(payload))
    receipt = {
        "created_utc": _utcnow_iso(),
        "target": slug,
        "file_count": len(files),
        "byte_count": byte_count,
        "oldest_mtime": min(mtimes),
        "newest_mtime": max(mtimes),
        "mode": "archive",
        "archive": archive_path.name,
        "class": klass,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
    }
    tmp = receipt_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True))
    os.replace(tmp, receipt_path)
    return archive_path


def _prune_files(
    cfg: RetentionConfig,
    report: LaneReport,
    slug: str,
    candidates: List[Path],
    archive_class: Optional[str],
) -> None:
    """Account candidates; on execute archive (unless class is None/cache)
    then unlink."""
    by_class: Dict[str, List[Path]] = {}
    for path in candidates:
        klass = archive_class if archive_class else _classify(path)
        by_class.setdefault(klass, []).append(path)
        report.candidate_files += 1
        report.candidate_bytes += _file_size(path)
    if not cfg.execute:
        return
    for klass, files in sorted(by_class.items()):
        if klass != CLASS_CACHE:
            archive = _write_archive(cfg, f"{slug}-{klass}", klass, files)
            if archive is not None:
                report.archives.append(archive.name)
        for path in files:
            size = _file_size(path)
            try:
                path.unlink()
            except OSError as exc:
                report.errors.append(f"unlink {path}: {exc}")
                continue
            report.acted_files += 1
            report.acted_bytes += size


# ---------------------------------------------------------------------------
# Lane: dispatch + gateway logs
# ---------------------------------------------------------------------------

def lane_dispatch_logs(cfg: RetentionConfig, home: Path) -> LaneReport:
    report = LaneReport(lane="dispatch-logs")
    if _lane_disabled("DISPATCH_LOGS"):
        report.enabled = False
        return report
    log_dirs = sorted(home.glob("state/*-agent-dispatch/logs"))
    log_dirs.append(home / "logs")
    for log_dir in log_dirs:
        if not log_dir.is_dir() or log_dir.is_symlink():
            continue
        if not _is_within(cfg.root, log_dir):
            report.notes.append(f"skipped outside-root target {log_dir}")
            continue
        files = list(_iter_regular_files(log_dir))
        candidates = _split_keep_min(files, cfg.keep_min, cfg.age_cutoff)
        if candidates:
            _prune_files(
                cfg, report, _slug_for(cfg.root, log_dir), candidates, None
            )
    return report


# ---------------------------------------------------------------------------
# Lane: orphaned lane workdirs
# ---------------------------------------------------------------------------

def _registry_says_alive(registry_path: Path, lane_id: str) -> bool:
    """Fail-safe registry check: if the registry mentions the lane id and we
    cannot positively determine every mention is dead, treat it as alive."""
    try:
        raw = registry_path.read_text(encoding="utf-8")
    except OSError:
        return False
    if lane_id not in raw:
        return False
    try:
        data = json.loads(raw)
    except ValueError:
        return True  # mentioned but unparseable => assume alive
    alive_flags: List[bool] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            values = json.dumps(node)
            if lane_id in values:
                for key in ("alive", "root_process_alive"):
                    if key in node:
                        alive_flags.append(bool(node[key]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    if not alive_flags:
        return True  # mentioned, no liveness verdict => assume alive
    return any(alive_flags)


def _process_sweep_mentions(needles: List[str]) -> bool:
    """Best-effort scan of live process command lines + env (own processes)
    for any needle, mirroring the HERMES_LANE_MARKER descendant sweep."""
    for args in (["ps", "-axwwE", "-o", "pid=,command="],
                 ["ps", "-axww", "-o", "pid=,command="]):
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=20
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode != 0:
            continue
        haystack = proc.stdout
        return any(needle in haystack for needle in needles)
    return False  # sweep unavailable; grace + registry remain the gate


def _git_snapshot(workdir: Path) -> List[Tuple[str, bytes]]:
    """Capture status/diff/HEAD of a workdir clone before removal."""
    members: List[Tuple[str, bytes]] = []
    base = f"lane-workdir-snapshots/{workdir.name}"
    for name, git_args in (
        ("status.txt", ["status", "--porcelain=v1", "--branch"]),
        ("diff.patch", ["diff"]),
        ("diff-cached.patch", ["diff", "--cached"]),
        ("head.txt", ["rev-parse", "HEAD"]),
    ):
        try:
            proc = subprocess.run(
                ["git", "-C", str(workdir), *git_args],
                capture_output=True,
                timeout=60,
            )
            payload = proc.stdout if proc.returncode == 0 else (
                b"<git error> " + proc.stderr
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            payload = f"<unavailable: {exc}>".encode()
        members.append((f"{base}/{name}", payload))
    return members


def lane_lane_workdirs(cfg: RetentionConfig, home: Path) -> LaneReport:
    report = LaneReport(lane="lane-workdirs")
    if _lane_disabled("LANE_WORKDIRS"):
        report.enabled = False
        return report
    grace_cutoff = time.time() - cfg.workdir_grace_hours * 3600.0
    for parent in sorted(home.glob("state/*-agent-dispatch/lane-workdirs")):
        if not parent.is_dir() or parent.is_symlink():
            continue
        registry = parent.parent / "agent-work-registry.json"
        for workdir in sorted(parent.iterdir()):
            try:
                if workdir.is_symlink() or not workdir.is_dir():
                    continue
            except OSError:
                continue
            if not _is_within(cfg.root, workdir):
                report.notes.append(f"skipped outside-root target {workdir}")
                continue
            lane_id = workdir.name
            if _file_mtime(workdir) > grace_cutoff:
                report.notes.append(f"{lane_id}: inside grace window")
                continue
            if registry.exists() and _registry_says_alive(registry, lane_id):
                report.notes.append(f"{lane_id}: registry says alive")
                continue
            if _process_sweep_mentions([lane_id, str(workdir)]):
                report.notes.append(f"{lane_id}: live process references lane")
                continue
            dir_bytes = sum(
                _file_size(p)
                for p in workdir.rglob("*")
                if not p.is_symlink() and p.is_file()
            )
            report.candidate_files += 1
            report.candidate_bytes += dir_bytes
            if not cfg.execute:
                continue
            receipts = [
                p
                for p in _iter_regular_files(workdir)
                if p.suffix.lower() in {".json", ".md"}
            ]
            extra = _git_snapshot(workdir)
            archive = _write_archive(
                cfg,
                f"lane-workdir-{lane_id}",
                CLASS_RECEIPT,
                receipts,
                extra_members=extra,
            )
            if archive is not None:
                report.archives.append(archive.name)
            try:
                shutil.rmtree(workdir)
            except OSError as exc:
                report.errors.append(f"rmtree {workdir}: {exc}")
                continue
            report.acted_files += 1
            report.acted_bytes += dir_bytes
    return report


# ---------------------------------------------------------------------------
# Lane: cron run output
# ---------------------------------------------------------------------------

def lane_cron_output(
    cfg: RetentionConfig, home: Path, lane_name: str = "cron-output"
) -> LaneReport:
    report = LaneReport(lane=lane_name)
    if _lane_disabled("CRON_OUTPUT"):
        report.enabled = False
        return report
    output_root = home / "cron" / "output"
    if not output_root.is_dir():
        return report
    for job_dir in sorted(output_root.iterdir()):
        try:
            if job_dir.is_symlink() or not job_dir.is_dir():
                continue
        except OSError:
            continue
        if not _is_within(cfg.root, job_dir):
            report.notes.append(f"skipped outside-root target {job_dir}")
            continue
        files = list(_iter_regular_files(job_dir))
        candidates = _split_keep_min(files, cfg.keep_min, cfg.age_cutoff)
        if candidates:
            _prune_files(
                cfg,
                report,
                _slug_for(cfg.root, job_dir),
                candidates,
                CLASS_BULK,
            )
        if cfg.execute:
            try:
                if not any(job_dir.iterdir()):
                    job_dir.rmdir()
            except OSError:
                pass
    return report


# ---------------------------------------------------------------------------
# Lane: state.db row retention + VACUUM
# ---------------------------------------------------------------------------

def _db_tables(conn: sqlite3.Connection) -> set:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    ).fetchall()
    return {row[0] for row in rows}


def _export_rows_gzip_jsonl(
    cfg: RetentionConfig, slug: str, klass: str, rows: List[Dict[str, Any]]
) -> Optional[Path]:
    if not rows:
        return None
    cfg.archive_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.archive_dir / f"{slug}-{cfg.runstamp}.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str) + "\n")
    receipt_path = cfg.archive_dir / f"{slug}-{cfg.runstamp}.receipt.json"
    receipt = {
        "created_utc": _utcnow_iso(),
        "target": slug,
        "file_count": len(rows),
        "byte_count": path.stat().st_size,
        "oldest_mtime": None,
        "newest_mtime": None,
        "mode": "archive",
        "archive": path.name,
        "class": klass,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
    }
    tmp = receipt_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True))
    os.replace(tmp, receipt_path)
    return path


def _eligible_session_ids(
    conn: sqlite3.Connection, cutoff: float
) -> List[str]:
    ended = conn.execute(
        """
        SELECT id FROM sessions
        WHERE COALESCE(ended_at, started_at) < ?
          AND (ended_at IS NOT NULL OR end_reason IS NOT NULL)
        """,
        (cutoff,),
    ).fetchall()
    stale_open = conn.execute(
        """
        SELECT s.id FROM sessions s
        WHERE s.started_at < ?
          AND s.ended_at IS NULL AND s.end_reason IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM messages m
              WHERE m.session_id = s.id AND m.timestamp >= ?
          )
        """,
        (cutoff, cutoff),
    ).fetchall()
    return [row[0] for row in ended] + [row[0] for row in stale_open]


def _retain_one_db(
    cfg: RetentionConfig, report: LaneReport, db_path: Path
) -> None:
    if not db_path.is_file() or db_path.is_symlink():
        return
    if not _is_within(cfg.root, db_path):
        report.notes.append(f"skipped outside-root db {db_path}")
        return
    db_size = _file_size(db_path)
    slug = _slug_for(cfg.root, db_path)
    uri = f"file:{db_path}?mode={'rw' if cfg.execute else 'ro'}"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=30)
    except sqlite3.Error as exc:
        report.errors.append(f"{slug}: connect failed: {exc}")
        return
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        tables = _db_tables(conn)
        if not {"sessions", "messages"} <= tables:
            report.notes.append(f"{slug}: no sessions/messages tables")
            return
        cutoff = cfg.db_cutoff
        session_ids = _eligible_session_ids(conn, cutoff)
        if session_ids:
            placeholders = ",".join("?" for _ in session_ids)
            msg_count, msg_bytes = conn.execute(
                f"""
                SELECT COUNT(*), COALESCE(SUM(LENGTH(COALESCE(content,''))
                    + LENGTH(COALESCE(reasoning_content,''))
                    + LENGTH(COALESCE(tool_calls,''))), 0)
                FROM messages WHERE session_id IN ({placeholders})
                """,
                session_ids,
            ).fetchone()
        else:
            msg_count, msg_bytes = 0, 0
        report.candidate_files += msg_count + len(session_ids)
        report.candidate_bytes += int(msg_bytes)
        report.notes.append(
            f"{slug}: {len(session_ids)} prunable sessions, "
            f"{msg_count} messages, db {db_size / 1048576:.0f} MiB"
        )
        if db_size > cfg.db_warn_bytes:
            report.over_threshold = True
        if not cfg.execute or not session_ids:
            return

        session_rows = []
        for start in range(0, len(session_ids), DB_DELETE_BATCH):
            batch = session_ids[start : start + DB_DELETE_BATCH]
            placeholders = ",".join("?" for _ in batch)
            cursor = conn.execute(
                f"SELECT * FROM sessions WHERE id IN ({placeholders})", batch
            )
            columns = [d[0] for d in cursor.description]
            session_rows.extend(
                dict(zip(columns, row)) for row in cursor.fetchall()
            )
        for row in session_rows:
            row.pop("system_prompt", None)  # bulk prompt text, not accounting
        _export_rows_gzip_jsonl(
            cfg, f"{slug}-sessions-summary", CLASS_RECEIPT, session_rows
        )

        exported = 0
        message_rows: List[Dict[str, Any]] = []
        for start in range(0, len(session_ids), DB_DELETE_BATCH):
            batch = session_ids[start : start + DB_DELETE_BATCH]
            placeholders = ",".join("?" for _ in batch)
            cursor = conn.execute(
                f"SELECT * FROM messages WHERE session_id IN ({placeholders})",
                batch,
            )
            columns = [d[0] for d in cursor.description]
            message_rows.extend(
                dict(zip(columns, row)) for row in cursor.fetchall()
            )
            exported += len(message_rows)
        archive = _export_rows_gzip_jsonl(
            cfg, f"{slug}-messages", CLASS_BULK, message_rows
        )
        if archive is not None:
            report.archives.append(archive.name)
        del message_rows

        deleted_messages = 0
        for start in range(0, len(session_ids), DB_DELETE_BATCH):
            batch = session_ids[start : start + DB_DELETE_BATCH]
            placeholders = ",".join("?" for _ in batch)
            with conn:
                cur = conn.execute(
                    f"DELETE FROM messages WHERE session_id IN ({placeholders})",
                    batch,
                )
                deleted_messages += cur.rowcount
                conn.execute(
                    f"""
                    UPDATE sessions SET parent_session_id = NULL
                    WHERE parent_session_id IN ({placeholders})
                    """,
                    batch,
                )
                conn.execute(
                    f"DELETE FROM sessions WHERE id IN ({placeholders})", batch
                )
        if "compression_locks" in tables:
            with conn:
                conn.execute(
                    "DELETE FROM compression_locks WHERE expires_at < ?",
                    (time.time(),),
                )
        report.acted_files += deleted_messages + len(session_ids)
        report.acted_bytes += int(msg_bytes)

        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            report.notes.append(f"{slug}: wal_checkpoint skipped: {exc}")
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        reclaimable = freelist * page_size
        if reclaimable < cfg.vacuum_threshold_bytes:
            report.notes.append(
                f"{slug}: VACUUM skipped, reclaimable "
                f"{reclaimable / 1048576:.0f} MiB below threshold"
            )
            return
        free_disk = shutil.disk_usage(str(db_path.parent)).free
        if free_disk < int(db_size * 2.2):
            report.notes.append(
                f"{slug}: VACUUM skipped, insufficient free disk"
            )
            return
        try:
            conn.execute("VACUUM")
            report.notes.append(
                f"{slug}: VACUUM reclaimed ~{reclaimable / 1048576:.0f} MiB"
            )
        except sqlite3.Error as exc:
            report.notes.append(f"{slug}: VACUUM aborted cleanly: {exc}")
    except sqlite3.Error as exc:
        report.errors.append(f"{slug}: {exc}")
    finally:
        conn.close()


def lane_state_db(cfg: RetentionConfig, home: Path) -> LaneReport:
    report = LaneReport(lane="state-db")
    if _lane_disabled("STATE_DB"):
        report.enabled = False
        return report
    for db_path in (
        home / "state.db",
        home / "sessions" / "state.db",
        home / "state" / "hermes" / "state.db",
    ):
        try:
            _retain_one_db(cfg, report, db_path)
        except Exception as exc:  # never-raise lane contract
            report.errors.append(f"{db_path}: {exc}")
    return report


# ---------------------------------------------------------------------------
# Lane: profile caches (re-derivable, deleted without archive)
# ---------------------------------------------------------------------------

def lane_profile_caches(cfg: RetentionConfig, home: Path) -> LaneReport:
    report = LaneReport(lane="profile-caches")
    if _lane_disabled("PROFILE_CACHES"):
        report.enabled = False
        return report
    cache_roots: List[Path] = [home / "lsp"]
    profiles_dir = home / "profiles"
    if profiles_dir.is_dir():
        for profile in sorted(profiles_dir.iterdir()):
            if profile.is_symlink() or not profile.is_dir():
                continue
            cache_roots.extend(
                [
                    profile / "lsp",
                    profile / "cache",
                    profile / "home" / ".cache",
                    *sorted(profile.glob("home/.npm*")),
                ]
            )
    for cache_root in cache_roots:
        if not cache_root.is_dir() or cache_root.is_symlink():
            continue
        if not _is_within(cfg.root, cache_root):
            report.notes.append(f"skipped outside-root target {cache_root}")
            continue
        candidates = [
            p
            for p in cache_root.rglob("*")
            if not p.is_symlink()
            and p.is_file()
            and _file_mtime(p) < cfg.age_cutoff
        ]
        if candidates:
            _prune_files(
                cfg,
                report,
                _slug_for(cfg.root, cache_root),
                candidates,
                CLASS_CACHE,
            )
        if cfg.execute:
            for directory in sorted(
                (
                    p
                    for p in cache_root.rglob("*")
                    if p.is_dir() and not p.is_symlink()
                ),
                key=lambda p: len(str(p)),
                reverse=True,
            ):
                try:
                    directory.rmdir()  # only succeeds when empty
                except OSError:
                    pass
    return report


# ---------------------------------------------------------------------------
# Lane: profiles (recurse mirrored shapes)
# ---------------------------------------------------------------------------

def lane_profiles(cfg: RetentionConfig) -> List[LaneReport]:
    if _lane_disabled("PROFILES"):
        return [LaneReport(lane="profiles", enabled=False)]
    reports: List[LaneReport] = []
    profiles_dir = cfg.root / "profiles"
    if not profiles_dir.is_dir():
        return reports
    for profile in sorted(profiles_dir.iterdir()):
        if profile.is_symlink() or not profile.is_dir():
            continue
        sub = lane_cron_output(
            cfg, profile, lane_name=f"cron-output[{profile.name}]"
        )
        reports.append(sub)
        db_report = LaneReport(lane=f"state-db[{profile.name}]")
        try:
            _retain_one_db(cfg, db_report, profile / "state.db")
        except Exception as exc:
            db_report.errors.append(f"{profile / 'state.db'}: {exc}")
        reports.append(db_report)
        logs_report = LaneReport(lane=f"logs[{profile.name}]")
        logs_dir = profile / "logs"
        if logs_dir.is_dir() and not logs_dir.is_symlink():
            files = list(_iter_regular_files(logs_dir))
            candidates = _split_keep_min(files, cfg.keep_min, cfg.age_cutoff)
            if candidates:
                _prune_files(
                    cfg,
                    logs_report,
                    _slug_for(cfg.root, logs_dir),
                    candidates,
                    CLASS_BULK,
                )
        reports.append(logs_report)
    return reports


# ---------------------------------------------------------------------------
# Lane: archive GC (bulk/cache archives age out; receipts never)
# ---------------------------------------------------------------------------

def lane_archive_gc(cfg: RetentionConfig) -> LaneReport:
    report = LaneReport(lane="archive-gc")
    if _lane_disabled("ARCHIVE_GC"):
        report.enabled = False
        return report
    archive_dir = cfg.archive_dir
    if not archive_dir.is_dir():
        return report
    cutoff = time.time() - cfg.bulk_archive_max_age_days * 86400.0
    for receipt_path in sorted(archive_dir.glob("*.receipt.json")):
        try:
            receipt = json.loads(receipt_path.read_text())
        except (OSError, ValueError):
            continue  # unreadable receipt => leave both files alone
        if receipt.get("class") == CLASS_RECEIPT:
            continue
        archive_name = receipt.get("archive")
        if not archive_name:
            continue
        archive_path = archive_dir / archive_name
        if not _is_within(archive_dir, archive_path):
            continue
        if _file_mtime(receipt_path) >= cutoff:
            continue
        size = _file_size(archive_path)
        report.candidate_files += 1
        report.candidate_bytes += size
        if not cfg.execute:
            continue
        try:
            if archive_path.exists():
                archive_path.unlink()
            receipt_path.unlink()
        except OSError as exc:
            report.errors.append(f"gc {archive_path}: {exc}")
            continue
        report.acted_files += 1
        report.acted_bytes += size
    return report


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_retention(cfg: RetentionConfig) -> List[LaneReport]:
    reports: List[LaneReport] = []
    if _env_flag("HERMES_HOME_RETENTION_DISABLED"):
        disabled = LaneReport(lane="all", enabled=False)
        disabled.notes.append("HERMES_HOME_RETENTION_DISABLED is set")
        return [disabled]
    for lane_fn in (
        lane_dispatch_logs,
        lane_lane_workdirs,
        lane_cron_output,
        lane_state_db,
        lane_profile_caches,
    ):
        try:
            reports.append(lane_fn(cfg, cfg.root))
        except Exception as exc:  # never-raise lane contract
            failed = LaneReport(lane=lane_fn.__name__)
            failed.errors.append(str(exc))
            reports.append(failed)
    try:
        reports.extend(lane_profiles(cfg))
    except Exception as exc:
        failed = LaneReport(lane="profiles")
        failed.errors.append(str(exc))
        reports.append(failed)
    try:
        reports.append(lane_archive_gc(cfg))
    except Exception as exc:
        failed = LaneReport(lane="archive-gc")
        failed.errors.append(str(exc))
        reports.append(failed)
    for report in reports:
        if (
            report.candidate_files > cfg.warn_files
            or report.candidate_bytes > cfg.warn_bytes
        ):
            report.over_threshold = True
    if cfg.execute:
        _write_run_receipt(cfg, reports)
    return reports


def _write_run_receipt(
    cfg: RetentionConfig, reports: List[LaneReport]
) -> None:
    runs_dir = cfg.root / RUNS_DIRNAME
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "hermes.home_retention_run.v1",
            "created_utc": _utcnow_iso(),
            "runstamp": cfg.runstamp,
            "root": str(cfg.root),
            "execute": cfg.execute,
            "lanes": [r.to_dict() for r in reports],
        }
        path = runs_dir / f"{cfg.runstamp}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        os.replace(tmp, path)
    except OSError:
        pass  # run receipt is best-effort


def format_report(cfg: RetentionConfig, reports: List[LaneReport]) -> str:
    mode = "EXECUTE" if cfg.execute else "DRY-RUN"
    lines = [f"{TOOL_NAME} {TOOL_VERSION} [{mode}] root={cfg.root}"]
    for report in reports:
        if not report.enabled:
            lines.append(f"  {report.lane}: DISABLED")
            continue
        flag = " OVER-THRESHOLD" if report.over_threshold else ""
        lines.append(
            f"  {report.lane}: candidates={report.candidate_files} "
            f"({report.candidate_bytes / 1048576:.1f} MiB) "
            f"acted={report.acted_files} "
            f"({report.acted_bytes / 1048576:.1f} MiB){flag}"
        )
        for note in report.notes:
            lines.append(f"    note: {note}")
        for error in report.errors:
            lines.append(f"    ERROR: {error}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog=TOOL_NAME, description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="hermes home to retain (default: get_hermes_home())",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--check",
        action="store_true",
        help="mutate nothing; exit 1 when any lane is over threshold",
    )
    parser.add_argument("--max-age-days", type=float, default=14.0)
    parser.add_argument("--keep-min", type=int, default=25)
    parser.add_argument("--workdir-grace-hours", type=float, default=24.0)
    parser.add_argument("--db-max-age-days", type=float, default=30.0)
    parser.add_argument(
        "--vacuum-threshold-bytes", type=int, default=256 * 1024 * 1024
    )
    parser.add_argument(
        "--bulk-archive-max-age-days", type=float, default=90.0
    )
    parser.add_argument("--warn-files", type=int, default=10_000)
    parser.add_argument(
        "--warn-bytes", type=int, default=500 * 1024 * 1024
    )
    parser.add_argument(
        "--db-warn-bytes", type=int, default=2 * 1024 * 1024 * 1024
    )
    args = parser.parse_args(argv)

    if args.check and args.execute:
        parser.error("--check and --execute are mutually exclusive")

    root = (args.root or _get_hermes_home()).expanduser()
    if not root.is_dir():
        print(f"{TOOL_NAME}: root {root} does not exist", file=sys.stderr)
        return 0  # nothing to retain is not a failure

    cfg = RetentionConfig(
        root=root,
        execute=bool(args.execute),
        max_age_days=args.max_age_days,
        keep_min=args.keep_min,
        workdir_grace_hours=args.workdir_grace_hours,
        db_max_age_days=args.db_max_age_days,
        vacuum_threshold_bytes=args.vacuum_threshold_bytes,
        bulk_archive_max_age_days=args.bulk_archive_max_age_days,
        warn_files=args.warn_files,
        warn_bytes=args.warn_bytes,
        db_warn_bytes=args.db_warn_bytes,
    )
    reports = run_retention(cfg)
    print(format_report(cfg, reports))
    if args.check:
        return 1 if any(r.over_threshold for r in reports) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
