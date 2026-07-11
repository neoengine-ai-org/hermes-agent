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
import itertools
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
    # keep dots: stripping them collides "a.b/logs" with "ab/logs"
    return str(rel).replace(os.sep, "-").lstrip(".")


def _unique_archive_base(cfg: RetentionConfig, slug: str) -> str:
    """Collision-safe archive basename within the archive dir. The receipt
    path is atomically reserved with O_EXCL, so two runs (or two slugs
    colliding in the same second) can never claim the same base."""
    base = f"{slug}-{cfg.runstamp}"
    candidate = base
    counter = 2
    while True:
        receipt = cfg.archive_dir / f"{candidate}.receipt.json"
        collision = (
            (cfg.archive_dir / f"{candidate}.tar.gz").exists()
            or (cfg.archive_dir / f"{candidate}.jsonl.gz").exists()
        )
        if not collision:
            try:
                fd = os.open(
                    str(receipt), os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                os.close(fd)
                return candidate
            except FileExistsError:
                pass
            except OSError:
                return candidate  # cannot reserve; fall back to name check
        candidate = f"{base}-{counter}"
        counter += 1


def _release_reservation(receipt_path: Path) -> None:
    """Remove an O_EXCL reservation receipt left behind by a failed archive
    write — an empty receipt must not leak (archive-gc skips unreadable
    receipts forever)."""
    try:
        if receipt_path.exists() and receipt_path.stat().st_size == 0:
            receipt_path.unlink()
    except OSError:
        pass


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_receipt(cfg: RetentionConfig, receipt_path: Path, receipt: Dict[str, Any]) -> None:
    tmp = receipt_path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(receipt, indent=2, sort_keys=True))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, receipt_path)
    _fsync_dir(receipt_path.parent)


def _write_archive(
    cfg: RetentionConfig,
    slug: str,
    klass: str,
    files: List[Path],
    extra_members: Optional[List[Tuple[str, bytes]]] = None,
) -> Tuple[Optional[Path], List[Path]]:
    """Create ``<slug>-<runstamp>.tar.gz`` + sibling receipt, durably
    (tmp-write, fsync, rename, dir fsync, readback verify).

    Returns ``(archive_path, verified_files)`` where ``verified_files`` are
    the files proven present in the readback — the ONLY files a caller may
    delete. Returns ``(None, [])`` when there is nothing to archive or the
    archive could not be verified.
    """
    if not files and not extra_members:
        return None, []
    cfg.archive_dir.mkdir(parents=True, exist_ok=True)
    base = _unique_archive_base(cfg, slug)
    archive_path = cfg.archive_dir / f"{base}.tar.gz"
    receipt_path = cfg.archive_dir / f"{base}.receipt.json"
    root_resolved = cfg.root.resolve()
    added: List[Tuple[str, Path]] = []
    tmp_archive = cfg.archive_dir / f"{base}.tar.gz.tmp"
    try:
        with open(tmp_archive, "wb") as fh:
            with tarfile.open(fileobj=fh, mode="w:gz") as tar:
                for path in files:
                    try:
                        arcname = str(
                            path.resolve().relative_to(root_resolved)
                        )
                    except ValueError:
                        arcname = path.name
                    try:
                        tar.add(path, arcname=arcname, recursive=False)
                        added.append((arcname, path))
                    except OSError:
                        continue
                for member_name, payload in extra_members or []:
                    info = tarfile.TarInfo(name=member_name)
                    info.size = len(payload)
                    info.mtime = int(time.time())
                    tar.addfile(info, io.BytesIO(payload))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_archive, archive_path)
        _fsync_dir(cfg.archive_dir)
    except OSError:
        try:
            tmp_archive.unlink()
        except OSError:
            pass
        _release_reservation(receipt_path)
        return None, []
    # readback: only files verifiably inside the archive may be deleted
    try:
        with tarfile.open(archive_path) as tar:
            present = set(tar.getnames())
    except (OSError, tarfile.TarError):
        _release_reservation(receipt_path)
        return None, []
    verified = [path for arcname, path in added if arcname in present]
    mtimes = [_file_mtime(p) for p in verified] or [time.time()]
    receipt = {
        "created_utc": _utcnow_iso(),
        "target": slug,
        "file_count": len(verified),
        "byte_count": sum(_file_size(p) for p in verified),
        "oldest_mtime": min(mtimes),
        "newest_mtime": max(mtimes),
        "mode": "archive",
        "archive": archive_path.name,
        "class": klass,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
    }
    try:
        _write_receipt(cfg, receipt_path, receipt)
    except OSError:
        _release_reservation(receipt_path)
        return None, []
    return archive_path, verified


def _safe_unlink(
    cfg: RetentionConfig,
    report: LaneReport,
    path: Path,
    expected: Optional[os.stat_result] = None,
) -> bool:
    """Unlink with a last-instant re-check: the path must still resolve
    inside the retention root (an ancestor swapped to a symlink since the
    scan drops containment), must not have been replaced with a symlink or
    fresh content, and — when the scan-time stat is provided — must be
    byte-identical in (size, mtime) to what was scanned/archived."""
    try:
        stat = path.lstat()
    except OSError:
        return False
    if not os.path.isfile(path) or path.is_symlink():
        report.notes.append(f"skipped {path.name}: replaced since scan")
        return False
    if not _is_within(cfg.root, path):
        report.notes.append(f"skipped {path.name}: left root since scan")
        return False
    if stat.st_mtime >= cfg.age_cutoff:
        report.notes.append(f"skipped {path.name}: fresh content since scan")
        return False
    if expected is not None and (
        stat.st_size != expected.st_size
        or stat.st_mtime_ns != expected.st_mtime_ns
        or stat.st_ino != expected.st_ino
        or stat.st_dev != expected.st_dev
    ):
        report.notes.append(f"skipped {path.name}: changed since scan")
        return False
    size = stat.st_size
    try:
        path.unlink()
    except OSError as exc:
        report.errors.append(f"unlink {path}: {exc}")
        return False
    report.acted_files += 1
    report.acted_bytes += size
    return True


def _prune_files(
    cfg: RetentionConfig,
    report: LaneReport,
    slug: str,
    candidates: List[Path],
    archive_class: Optional[str],
) -> None:
    """Account candidates; on execute archive (unless cache class) then
    unlink ONLY the files verified inside the archive."""
    by_class: Dict[str, List[Path]] = {}
    scan_stats: Dict[Path, os.stat_result] = {}
    for path in candidates:
        klass = archive_class if archive_class else _classify(path)
        by_class.setdefault(klass, []).append(path)
        try:
            scan_stats[path] = path.lstat()
        except OSError:
            continue
        report.candidate_files += 1
        report.candidate_bytes += scan_stats[path].st_size
    if not cfg.execute:
        return
    for klass, files in sorted(by_class.items()):
        files = [p for p in files if p in scan_stats]
        if klass == CLASS_CACHE:
            deletable = files  # re-derivable, no archive required
        else:
            archive, deletable = _write_archive(
                cfg, f"{slug}-{klass}", klass, files
            )
            if archive is not None:
                report.archives.append(archive.name)
            unverified = len(files) - len(deletable)
            if unverified:
                report.notes.append(
                    f"{slug}-{klass}: {unverified} file(s) kept — not "
                    "verified in archive"
                )
        for path in deletable:
            _safe_unlink(cfg, report, path, expected=scan_stats.get(path))


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
        return True  # registry exists but is unreadable => assume alive
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


def _process_sweep_mentions(needles: List[str]) -> Optional[bool]:
    """Best-effort scan of live process command lines + env (own processes)
    for any needle, mirroring the HERMES_LANE_MARKER descendant sweep.

    Returns True/False on a successful sweep, None when the sweep itself is
    unavailable — callers must treat None as potentially-live (fail-safe).
    """
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
    return None  # sweep unavailable => caller must fail safe


_GIT_SNAPSHOT_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}


def _run_git(workdir: Path, args: List[str]) -> Tuple[int, bytes, bytes]:
    """Run git read-only in a workdir. GIT_OPTIONAL_LOCKS=0 stops `git
    status` from refreshing .git/index — otherwise our own snapshot would
    trip the nested-mtime revival re-check."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(workdir), *args],
            capture_output=True,
            timeout=60,
            env=_GIT_SNAPSHOT_ENV,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, b"", f"<unavailable: {exc}>".encode()


def _git_snapshot(
    workdir: Path,
) -> Tuple[List[Tuple[str, bytes]], bool, bool]:
    """Capture status/diff/HEAD/unpushed/stash state of a workdir clone
    before removal.

    Returns ``(members, is_dirty, stash_present)`` — dirty means the
    working tree holds ANY content a fresh clone could not reproduce:
    uncommitted tracked changes, untracked or ignored files, stash
    entries, or commits that exist on no remote (committed-but-unpushed
    work, including detached HEAD). Dirty workdirs must be archived as
    full working-tree content; when a stash exists the archive must also
    include ``.git`` itself, because ``stash -u`` untracked/binary content
    is representable in no patch output.
    """
    members: List[Tuple[str, bytes]] = []
    base = f"lane-workdir-snapshots/{workdir.name}"
    dirty = False
    stash_present = False
    for name, git_args, dirty_when in (
        # --ignored: ignored artifacts (build outputs, local results) count
        # as dirty too — a diff cannot reproduce them either
        ("status.txt",
         ["status", "--porcelain=v1", "--branch", "--ignored=matching"],
         "non-branch-lines"),
        ("diff.patch", ["diff"], None),
        ("diff-cached.patch", ["diff", "--cached"], None),
        ("head.txt", ["rev-parse", "HEAD"], None),
        # commits on no remote: a deleted clone is their ONLY copy.
        # HEAD is included explicitly so detached-HEAD commits count too.
        ("unpushed.txt",
         ["log", "HEAD", "--branches", "--not", "--remotes",
          "--format=%H %s"],
         "any-output"),
        ("stash.txt", ["stash", "list"], "any-output"),
        # stash diffs live only in .git (excluded from content archives) —
        # capture every entry's patch in the snapshot itself
        ("stash.patch", ["stash", "list", "-p"], None),
    ):
        returncode, stdout, stderr = _run_git(workdir, git_args)
        payload = stdout if returncode == 0 else b"<git error> " + stderr
        if dirty_when is not None:
            if returncode != 0:
                dirty = True  # cannot prove clean => treat as dirty
            elif dirty_when == "non-branch-lines":
                dirty = dirty or any(
                    line and not line.startswith("##")
                    for line in stdout.decode("utf-8", "replace").splitlines()
                )
            elif dirty_when == "any-output":
                dirty = dirty or bool(stdout.strip())
        if name == "stash.txt":
            stash_present = returncode != 0 or bool(stdout.strip())
        members.append((f"{base}/{name}", payload))
    return members, dirty, stash_present


def _workdir_content_files(
    workdir: Path,
    max_file_bytes: int = 64 * 1024 * 1024,
    include_git: bool = False,
) -> Tuple[List[Path], List[str]]:
    """All regular files in a workdir for full-content archiving of dirty
    workdirs. ``.git`` is excluded by default (the working tree + diffs
    reproduce it) but INCLUDED when the caller detects state that lives
    only inside .git — e.g. stash entries, whose ``-u`` untracked/binary
    content no patch output can carry. Oversized files are reported, not
    silently dropped."""
    files: List[Path] = []
    skipped: List[str] = []
    for path in sorted(workdir.rglob("*")):
        try:
            if path.is_symlink() or not path.is_file():
                continue
        except OSError:
            continue
        if not include_git and ".git" in path.relative_to(workdir).parts:
            continue
        if _file_size(path) > max_file_bytes:
            skipped.append(str(path.relative_to(workdir)))
            continue
        files.append(path)
    return files, skipped


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
            sweep = _process_sweep_mentions([lane_id, str(workdir)])
            if sweep is None:
                report.notes.append(
                    f"{lane_id}: process sweep unavailable — kept (fail-safe)"
                )
                continue
            if sweep:
                report.notes.append(f"{lane_id}: live process references lane")
                continue
            dir_bytes = 0
            newest_mtime = _file_mtime(workdir)
            for p in workdir.rglob("*"):
                try:
                    if p.is_symlink() or not p.is_file():
                        continue
                except OSError:
                    continue
                dir_bytes += _file_size(p)
                # .git metadata churn is not lane work-product (and our own
                # snapshot's `git status` may refresh .git/index) — real
                # activity shows in working-tree files, registry, or ps
                if ".git" in p.relative_to(workdir).parts:
                    continue
                newest_mtime = max(newest_mtime, _file_mtime(p))
            if newest_mtime > grace_cutoff:
                # a nested write does not bump the top-level dir mtime —
                # any file written inside the grace window means live
                report.notes.append(
                    f"{lane_id}: nested content inside grace window"
                )
                continue
            report.candidate_files += 1
            report.candidate_bytes += dir_bytes
            if not cfg.execute:
                continue
            snapshot, dirty, stash_present = _git_snapshot(workdir)
            if dirty:
                # diffs do not carry untracked/ignored contents — archive
                # the full working tree before removal; stash entries live
                # only in .git, so a stash pulls .git into the archive too
                to_archive, oversized = _workdir_content_files(
                    workdir, include_git=stash_present
                )
                for rel in oversized:
                    report.notes.append(
                        f"{lane_id}: oversized file kept out of archive: {rel}"
                    )
            else:
                to_archive = [
                    p
                    for p in _iter_regular_files(workdir)
                    if p.suffix.lower() in {".json", ".md"}
                ]
                oversized = []
            archive, verified = _write_archive(
                cfg,
                f"lane-workdir-{lane_id}",
                CLASS_RECEIPT,
                to_archive,
                extra_members=snapshot,
            )
            if archive is None:
                report.notes.append(
                    f"{lane_id}: kept — archive could not be written/verified"
                )
                continue
            report.archives.append(archive.name)
            if len(verified) < len(to_archive) or oversized:
                report.notes.append(
                    f"{lane_id}: kept — archive incomplete "
                    f"({len(verified)}/{len(to_archive)} verified, "
                    f"{len(oversized)} oversized)"
                )
                continue
            # last-instant re-check: lane may have revived since eligibility
            # (registry, nested newest mtime — a worker may have written a
            # result since the scan — AND a fresh process sweep)
            recheck_mtime = _file_mtime(workdir)
            for p in workdir.rglob("*"):
                try:
                    if p.is_symlink() or not p.is_file():
                        continue
                except OSError:
                    continue
                if ".git" in p.relative_to(workdir).parts:
                    continue  # our own snapshot refreshes .git/index
                recheck_mtime = max(recheck_mtime, _file_mtime(p))
            resweep = _process_sweep_mentions([lane_id, str(workdir)])
            if (
                recheck_mtime > grace_cutoff
                or (
                    registry.exists()
                    and _registry_says_alive(registry, lane_id)
                )
                or resweep is None
                or resweep
            ):
                report.notes.append(f"{lane_id}: revived before removal — kept")
                continue
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
    cfg: RetentionConfig,
    slug: str,
    klass: str,
    rows: Iterable[Dict[str, Any]],
) -> Tuple[str, Optional[Path], int]:
    """Durable gzipped-JSONL export (tmp-write, fsync, rename, dir fsync).
    Streams the iterable — never materializes the full row set in memory.
    Returns ``(status, path, count)`` with status one of ``ok`` / ``empty``
    / ``failed``. On ``failed`` callers must NOT delete the source rows."""
    iterator = iter(rows)
    try:
        first = next(iterator)
    except StopIteration:
        return "empty", None, 0
    cfg.archive_dir.mkdir(parents=True, exist_ok=True)
    base = _unique_archive_base(cfg, slug)
    path = cfg.archive_dir / f"{base}.jsonl.gz"
    tmp = cfg.archive_dir / f"{base}.jsonl.gz.tmp"
    count = 0
    try:
        with open(tmp, "wb") as raw:
            with gzip.open(raw, "wt", encoding="utf-8") as handle:
                for row in itertools.chain([first], iterator):
                    handle.write(json.dumps(row, default=str) + "\n")
                    count += 1
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(tmp, path)
        _fsync_dir(cfg.archive_dir)
        receipt = {
            "created_utc": _utcnow_iso(),
            "target": slug,
            "file_count": count,
            "byte_count": path.stat().st_size,
            "oldest_mtime": None,
            "newest_mtime": None,
            "mode": "archive",
            "archive": path.name,
            "class": klass,
            "tool": TOOL_NAME,
            "tool_version": TOOL_VERSION,
        }
        _write_receipt(cfg, cfg.archive_dir / f"{base}.receipt.json", receipt)
    except (OSError, sqlite3.Error):
        try:
            tmp.unlink()
        except OSError:
            pass
        _release_reservation(cfg.archive_dir / f"{base}.receipt.json")
        return "failed", None, count
    return "ok", path, count


def _session_referencing_tables(
    conn: sqlite3.Connection, tables: set
) -> Optional[List[Tuple[str, str]]]:
    """(table, fk_column) pairs for every table with a foreign key onto
    sessions, discovered from the live schema so drift is tolerated.
    ``messages`` is excluded — it has its own export. Returns None when
    discovery fails — callers must then abort deletion (fail closed): a
    table dropped from discovery would be cascade-deleted unexported."""
    refs: List[Tuple[str, str]] = []
    for table in sorted(tables):
        if table in {"sessions", "messages"} or table.startswith("sqlite_"):
            continue
        ident = '"' + table.replace('"', '""') + '"'
        try:
            rows = conn.execute(
                f"PRAGMA foreign_key_list({ident})"
            ).fetchall()
        except sqlite3.Error:
            return None
        for row in rows:
            # row: (id, seq, table, from, to, on_update, on_delete, match)
            if row[2] == "sessions":
                refs.append((table, row[3]))
    return refs


def _cascade_offenders(
    conn: sqlite3.Connection, tables: set, parents: set
) -> Optional[List[str]]:
    """Tables whose ON DELETE CASCADE reference onto a ``parents`` table
    would fire TRANSITIVELY when we delete sessions/messages rows —
    depth-2+ descendants we do not export. Non-empty means deletion must
    abort (extend the exporter first). None means discovery itself failed
    (cannot prove safety => also abort)."""
    offenders: List[str] = []
    for table in sorted(tables):
        if table in {"sessions", "messages"} or table.startswith("sqlite_"):
            continue
        ident = '"' + table.replace('"', '""') + '"'
        try:
            rows = conn.execute(
                f"PRAGMA foreign_key_list({ident})"
            ).fetchall()
        except sqlite3.Error:
            return None
        for row in rows:
            # row: (id, seq, table, from, to, on_update, on_delete, match)
            if row[2] in parents and str(row[6]).upper() == "CASCADE":
                offenders.append(f"{table}->{row[2]}")
    return offenders


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
        if cfg.execute:
            # the runtime schema declares ON DELETE CASCADE references to
            # sessions (e.g. telegram bindings) — honor them
            conn.execute("PRAGMA foreign_keys=ON")
        tables = _db_tables(conn)
        if not {"sessions", "messages"} <= tables:
            report.notes.append(f"{slug}: no sessions/messages tables")
            return
        cutoff = cfg.db_cutoff
        session_ids = _eligible_session_ids(conn, cutoff)
        msg_count, msg_bytes = 0, 0
        for start in range(0, len(session_ids), DB_DELETE_BATCH):
            batch = session_ids[start : start + DB_DELETE_BATCH]
            placeholders = ",".join("?" for _ in batch)
            row = conn.execute(
                f"""
                SELECT COUNT(*), COALESCE(SUM(LENGTH(COALESCE(content,''))
                    + LENGTH(COALESCE(reasoning_content,''))
                    + LENGTH(COALESCE(tool_calls,''))), 0)
                FROM messages WHERE session_id IN ({placeholders})
                """,
                batch,
            ).fetchone()
            msg_count += row[0]
            msg_bytes += row[1]
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
        summary_status, _, _ = _export_rows_gzip_jsonl(
            cfg, f"{slug}-sessions-summary", CLASS_RECEIPT, session_rows
        )
        if summary_status == "failed":
            report.errors.append(
                f"{slug}: sessions-summary export failed — rows NOT deleted"
            )
            return

        # foreign_keys=ON makes ON DELETE CASCADE references (e.g. telegram
        # topic bindings) follow the session delete — export them first,
        # discovered generically from the live schema. Discovery failure
        # aborts (fail closed), same as export failure.
        ref_tables = _session_referencing_tables(conn, tables)
        if ref_tables is None:
            report.errors.append(
                f"{slug}: FK reference discovery failed — rows NOT deleted"
            )
            return
        # a grandchild table cascading off a direct reference (or off
        # messages) would be deleted UNEXPORTED — fail closed until the
        # exporter is extended for that schema
        offenders = _cascade_offenders(
            conn, tables, {t for t, _ in ref_tables} | {"messages"}
        )
        if offenders is None:
            report.errors.append(
                f"{slug}: cascade-safety discovery failed — rows NOT deleted"
            )
            return
        if offenders:
            report.errors.append(
                f"{slug}: transitive ON DELETE CASCADE would delete "
                f"unexported rows ({', '.join(offenders)}) — rows NOT "
                "deleted; extend the exporter for these tables"
            )
            return
        ref_rows: List[Dict[str, Any]] = []
        for table, column in ref_tables:
            for start in range(0, len(session_ids), DB_DELETE_BATCH):
                batch = session_ids[start : start + DB_DELETE_BATCH]
                placeholders = ",".join("?" for _ in batch)
                try:
                    cursor = conn.execute(
                        f'SELECT * FROM "{table}" '
                        f'WHERE "{column}" IN ({placeholders})',
                        batch,
                    )
                except sqlite3.Error as exc:
                    # fail CLOSED: an unreadable reference table means the
                    # cascade would delete rows we could not export
                    report.errors.append(
                        f"{slug}: cannot export {table} refs ({exc}) — "
                        "rows NOT deleted"
                    )
                    return
                columns = [d[0] for d in cursor.description]
                ref_rows.extend(
                    {"_table": table, **dict(zip(columns, row))}
                    for row in cursor.fetchall()
                )
        refs_status, _, _ = _export_rows_gzip_jsonl(
            cfg, f"{slug}-session-refs", CLASS_RECEIPT, ref_rows
        )
        if refs_status == "failed":
            report.errors.append(
                f"{slug}: session-refs export failed — rows NOT deleted"
            )
            return

        # any message inserted after this point has a larger rowid, even if
        # its timestamp is old (replays) — used by delete revalidation
        snapshot_max_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM messages"
        ).fetchone()[0]

        def _iter_message_rows() -> Iterable[Dict[str, Any]]:
            # streamed: the first execute run exports multi-GiB of message
            # payload — materializing it as dicts would spike RSS
            for start in range(0, len(session_ids), DB_DELETE_BATCH):
                batch = session_ids[start : start + DB_DELETE_BATCH]
                placeholders = ",".join("?" for _ in batch)
                cursor = conn.execute(
                    "SELECT * FROM messages "
                    f"WHERE session_id IN ({placeholders})",
                    batch,
                )
                columns = [d[0] for d in cursor.description]
                while True:
                    rows = cursor.fetchmany(1000)
                    if not rows:
                        break
                    for row in rows:
                        yield dict(zip(columns, row))

        msg_status, archive, _ = _export_rows_gzip_jsonl(
            cfg, f"{slug}-messages", CLASS_BULK, _iter_message_rows()
        )
        if archive is not None:
            report.archives.append(archive.name)
        if msg_status == "failed":
            report.errors.append(
                f"{slug}: messages export failed — rows NOT deleted"
            )
            return

        deleted_messages = 0
        deleted_sessions = 0
        revived_sessions = 0
        for start in range(0, len(session_ids), DB_DELETE_BATCH):
            batch = session_ids[start : start + DB_DELETE_BATCH]
            placeholders = ",".join("?" for _ in batch)
            with conn:
                # revalidate inside the delete transaction: a session that
                # gained ANY message since the export snapshot (fresh
                # timestamp OR replayed old timestamp = rowid past the
                # snapshot), or whose lifecycle no longer satisfies
                # eligibility (reopened), is live again — keep it
                revived = {
                    r[0]
                    for r in conn.execute(
                        f"""
                        SELECT DISTINCT session_id FROM messages
                        WHERE session_id IN ({placeholders})
                          AND (timestamp >= ? OR id > ?)
                        """,
                        [*batch, cutoff, snapshot_max_id],
                    ).fetchall()
                }
                still_eligible = {
                    r[0]
                    for r in conn.execute(
                        f"""
                        SELECT id FROM sessions
                        WHERE id IN ({placeholders})
                          AND (
                            (
                              (ended_at IS NOT NULL OR end_reason IS NOT NULL)
                              AND COALESCE(ended_at, started_at) < ?
                            )
                            OR (
                              ended_at IS NULL AND end_reason IS NULL
                              AND started_at < ?
                            )
                          )
                        """,
                        [*batch, cutoff, cutoff],
                    ).fetchall()
                }
                keep = [
                    i
                    for i in batch
                    if i not in revived and i in still_eligible
                ]
                revived_sessions += len(batch) - len(keep)
                if not keep:
                    continue
                keep_ph = ",".join("?" for _ in keep)
                cur = conn.execute(
                    f"DELETE FROM messages WHERE session_id IN ({keep_ph})",
                    keep,
                )
                deleted_messages += cur.rowcount
                conn.execute(
                    f"""
                    UPDATE sessions SET parent_session_id = NULL
                    WHERE parent_session_id IN ({keep_ph})
                    """,
                    keep,
                )
                cur = conn.execute(
                    f"DELETE FROM sessions WHERE id IN ({keep_ph})", keep
                )
                deleted_sessions += cur.rowcount
        if revived_sessions:
            report.notes.append(
                f"{slug}: {revived_sessions} session(s) revived since "
                "selection — kept"
            )
        if "compression_locks" in tables:
            with conn:
                conn.execute(
                    "DELETE FROM compression_locks WHERE expires_at < ?",
                    (time.time(),),
                )
        report.acted_files += deleted_messages + deleted_sessions
        report.acted_bytes += int(msg_bytes)  # pre-delete estimate

        try:
            ckpt = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if ckpt and ckpt[0] == 1:
                report.notes.append(
                    f"{slug}: wal_checkpoint busy (writer active) — "
                    "WAL truncation deferred"
                )
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
    if profiles_dir.is_symlink() or not profiles_dir.is_dir():
        return reports
    if not _is_within(cfg.root, profiles_dir):
        return reports
    for profile in sorted(profiles_dir.iterdir()):
        if profile.is_symlink() or not profile.is_dir():
            continue
        if not _is_within(cfg.root, profile):
            continue
        sub = lane_cron_output(
            cfg, profile, lane_name=f"cron-output[{profile.name}]"
        )
        reports.append(sub)
        # per-lane kill switches gate profile recursion too
        db_report = LaneReport(lane=f"state-db[{profile.name}]")
        if _lane_disabled("STATE_DB"):
            db_report.enabled = False
        else:
            try:
                _retain_one_db(cfg, db_report, profile / "state.db")
            except Exception as exc:
                db_report.errors.append(f"{profile / 'state.db'}: {exc}")
        reports.append(db_report)
        logs_report = LaneReport(lane=f"logs[{profile.name}]")
        if _lane_disabled("DISPATCH_LOGS"):
            logs_report.enabled = False
        else:
            logs_dir = profile / "logs"
            if (
                logs_dir.is_dir()
                and not logs_dir.is_symlink()
                and _is_within(cfg.root, logs_dir)
            ):
                files = list(_iter_regular_files(logs_dir))
                candidates = _split_keep_min(
                    files, cfg.keep_min, cfg.age_cutoff
                )
                if candidates:
                    # None => per-file classifier, mirroring dispatch-logs
                    # (*.md closeouts stay receipt-class in profiles too)
                    _prune_files(
                        cfg,
                        logs_report,
                        _slug_for(cfg.root, logs_dir),
                        candidates,
                        None,
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
        if receipt.get("class") not in {CLASS_BULK, CLASS_CACHE}:
            continue  # unknown/missing class defaults to KEEP, like receipts
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
    # single-instance guard for mutating runs: two concurrent sweeps over
    # the same root would race each other's archives and re-checks
    # (--check stays lock-free: it mutates nothing, not even a lock file)
    lock_handle = None
    if cfg.execute:
        try:
            import fcntl  # POSIX-only; retention timers are POSIX-side

            lock_dir = root / "state" / "hermes-home-retention"
            lock_dir.mkdir(parents=True, exist_ok=True)
            lock_handle = open(lock_dir / ".lock", "w", encoding="utf-8")
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            pass
        except BlockingIOError:
            print(
                f"{TOOL_NAME}: another retention run holds the lock — exiting"
            )
            return 0
        except OSError as exc:
            # lock SETUP failure is not contention — surface it loudly so
            # the wrapper writes a handoff instead of silently skipping
            print(f"{TOOL_NAME}: lock setup failed: {exc}", file=sys.stderr)
            return 2

    try:
        reports = run_retention(cfg)
    finally:
        if lock_handle is not None:
            lock_handle.close()
    print(format_report(cfg, reports))
    if args.check:
        return 1 if any(r.over_threshold for r in reports) else 0
    if cfg.execute and any(r.errors for r in reports):
        # lane errors never raise, but an execute run that hit any must
        # exit non-zero so the cron wrapper writes a handoff alert
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
