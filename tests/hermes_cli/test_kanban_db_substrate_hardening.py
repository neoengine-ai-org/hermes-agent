"""Tests for the kanban SQLite substrate hardening in kanban_db.

Covers the two guards added after the 2026-06-24 ("orphan index") and
2026-07-06 ("Rowid out of order") page-corruption incidents:

* Periodic integrity re-probe: a long-lived process that connected before
  corruption happened must re-detect it instead of trusting the
  ``_INITIALIZED_PATHS`` cache forever.
* ``.corrupt.*`` backup cap: crash-looping connects against a damaged board
  must not fill the disk with hundreds of backup copies.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Helpers (corruption shape mirrors test_kanban_db.py's _write_corrupt_db)
# ---------------------------------------------------------------------------

def _write_corrupt_db(path: Path) -> bytes:
    """Valid SQLite header + malformed page content.

    Passes the cheap byte-header check but fails ``PRAGMA quick_check`` /
    ``integrity_check`` — the corruption shape both incidents produced.
    """
    header = b"SQLite format 3\x00" + b"\x10\x00\x02\x02\x00\x40\x20\x20"
    header += b"\x00\x00\x00\x0c\x00\x00\x23\x46\x00\x00\x00\x00"
    header = header.ljust(100, b"\x00")
    payload = b"definitely not a valid sqlite page \x00\x01\x02\x03" * 64
    blob = header + payload
    path.write_bytes(blob)
    return blob


def _fresh_connected_db(tmp_path: Path) -> Path:
    """Create a healthy kanban DB and leave its path trusted in the cache."""
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    assert str(db_path.resolve()) in kb._INITIALIZED_PATHS
    return db_path


def _corrupt_in_place(db_path: Path) -> None:
    """Simulate on-disk corruption happening while the path is cache-trusted."""
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.parent / (db_path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
    _write_corrupt_db(db_path)


def _copied_wal_db_without_shm(tmp_path: Path) -> Path:
    """Create a DB copy with WAL frames present but no copied SHM sidecar."""
    src = tmp_path / "source.db"
    db_path = tmp_path / "kanban.db"
    conn = sqlite3.connect(str(src))
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE seed(id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO seed(value) VALUES ('wal')")
        conn.commit()
        wal_path = src.parent / (src.name + "-wal")
        if not wal_path.exists():
            pytest.skip("sqlite build did not leave a WAL sidecar to copy")
        shutil.copy2(src, db_path)
        shutil.copy2(wal_path, db_path.parent / (db_path.name + "-wal"))
    finally:
        conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Periodic integrity re-probe
# ---------------------------------------------------------------------------

def test_first_connect_integrity_guard_connection_is_read_only(tmp_path, monkeypatch):
    """The first-connect full integrity guard must not open the live DB RW."""
    db_path = tmp_path / "kanban.db"
    seed = sqlite3.connect(str(db_path))
    try:
        seed.execute("CREATE TABLE seed(id INTEGER PRIMARY KEY)")
        seed.commit()
    finally:
        seed.close()
    key = str(db_path.resolve())
    kb._INITIALIZED_PATHS.discard(key)
    kb._LAST_INTEGRITY_PROBE.pop(key, None)

    calls: list[tuple[tuple, dict]] = []
    real_connect = sqlite3.connect

    def capturing_connect(*args, **kwargs):
        calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(kb.sqlite3, "connect", capturing_connect)
    conn = kb.connect(db_path=db_path)
    conn.close()

    assert len(calls) >= 2, f"expected guard + real connection, saw {calls!r}"
    guard_args, guard_kwargs = calls[0]
    assert guard_args[0].startswith("file:")
    assert "mode=ro" in guard_args[0]
    assert guard_kwargs.get("uri") is True

    ro = real_connect(guard_args[0], **guard_kwargs)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro.execute("CREATE TABLE guard_should_not_write(x)")
    finally:
        ro.close()


def test_first_connect_integrity_guard_does_not_create_sidecars(tmp_path):
    """A read-only guard must not create WAL/SHM sidecars during diagnosis."""
    db_path = tmp_path / "kanban.db"
    seed = sqlite3.connect(str(db_path))
    try:
        seed.execute("CREATE TABLE seed(id INTEGER PRIMARY KEY)")
        seed.commit()
    finally:
        seed.close()

    kb._guard_existing_db_is_healthy(db_path)

    assert not (tmp_path / "kanban.db-wal").exists()
    assert not (tmp_path / "kanban.db-shm").exists()


def test_first_connect_integrity_guard_does_not_create_sidecars_for_wal_db(tmp_path):
    """Even a WAL-shaped DB copy must not get sidecars from the read-only guard."""
    db_path = _copied_wal_db_without_shm(tmp_path)

    kb._guard_existing_db_is_healthy(db_path)

    assert (tmp_path / "kanban.db-wal").exists()
    assert not (tmp_path / "kanban.db-shm").exists()


def test_first_connect_read_only_probe_failure_verifies_rw_connection(
    tmp_path, monkeypatch
):
    """If a WAL edge refuses mode=ro, connect verifies the RW handle before init."""
    db_path = tmp_path / "kanban.db"
    seed = sqlite3.connect(str(db_path))
    try:
        seed.execute("CREATE TABLE seed(id INTEGER PRIMARY KEY, value TEXT)")
        seed.execute("INSERT INTO seed(value) VALUES ('ok')")
        seed.commit()
    finally:
        seed.close()
    key = str(db_path.resolve())
    kb._INITIALIZED_PATHS.discard(key)
    kb._LAST_INTEGRITY_PROBE.pop(key, None)

    real_connect = sqlite3.connect
    integrity_calls: list[str] = []
    real_integrity_check_reason = kb._integrity_check_reason

    def flaky_ro_connect(*args, **kwargs):
        target = str(args[0]) if args else ""
        if target.startswith("file:") and "mode=ro" in target:
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(*args, **kwargs)

    def spy_integrity_check(conn, *, pragma="integrity_check"):
        integrity_calls.append(pragma)
        return real_integrity_check_reason(conn, pragma=pragma)

    monkeypatch.setattr(kb.sqlite3, "connect", flaky_ro_connect)
    monkeypatch.setattr(kb, "_integrity_check_reason", spy_integrity_check)

    conn = kb.connect(db_path=db_path)
    try:
        assert conn.execute("SELECT value FROM seed").fetchone()[0] == "ok"
    finally:
        conn.close()

    assert integrity_calls == ["integrity_check"]
    assert key in kb._INITIALIZED_PATHS


def test_first_connect_read_only_probe_failure_detects_rw_corruption(
    tmp_path, monkeypatch
):
    """The RW fallback must still refuse a corrupt DB before schema migration."""
    db_path = tmp_path / "kanban.db"
    _write_corrupt_db(db_path)
    key = str(db_path.resolve())
    kb._INITIALIZED_PATHS.discard(key)
    kb._LAST_INTEGRITY_PROBE.pop(key, None)

    real_connect = sqlite3.connect

    def flaky_ro_connect(*args, **kwargs):
        target = str(args[0]) if args else ""
        if target.startswith("file:") and "mode=ro" in target:
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(kb.sqlite3, "connect", flaky_ro_connect)

    with pytest.raises(kb.KanbanDbCorruptError) as excinfo:
        kb.connect(db_path=db_path)

    err = excinfo.value
    assert "integrity_check" in err.reason or "sqlite refused" in err.reason
    assert err.backup_path is not None
    assert err.backup_path.exists()
    assert key not in kb._INITIALIZED_PATHS


def test_periodic_reprobe_detects_corruption_after_first_connect(
    tmp_path, monkeypatch
):
    """The exact incident shape: connect once (healthy), corrupt on disk,
    connect again after the recheck interval → must raise and evict."""
    monkeypatch.setenv("HERMES_KANBAN_INTEGRITY_RECHECK_SECONDS", "0.000001")
    db_path = _fresh_connected_db(tmp_path)
    key = str(db_path.resolve())

    _corrupt_in_place(db_path)
    time.sleep(0.01)  # let the tiny recheck interval elapse

    with pytest.raises(kb.KanbanDbCorruptError) as excinfo:
        kb.connect(db_path=db_path)

    err = excinfo.value
    # Periodic re-probe now runs the full integrity_check (not quick_check) so
    # it catches index/table divergence ("orphan index") too.
    assert "integrity_check" in err.reason or "sqlite refused" in err.reason
    # Cache evicted so subsequent connects re-run the full first-connect guard.
    assert key not in kb._INITIALIZED_PATHS
    # The existing backup path still ran.
    assert err.backup_path is not None
    assert err.backup_path.exists()

    # A follow-up connect goes through the FULL guard (not the cache) and
    # still refuses — no silent schema recreation on the damaged file.
    with pytest.raises(kb.KanbanDbCorruptError):
        kb.connect(db_path=db_path)


def test_index_only_integrity_corruption_is_reindexed_without_backup(
    tmp_path, monkeypatch
):
    """The observed lane-heartbeat index divergence should self-heal.

    SQLite ``integrity_check`` can report a table/index divergence such as
    ``row 15 missing from index idx_lane_heartbeats_state`` even though the
    table pages are intact. That class is repairable with ``REINDEX`` and must
    not fall into the generic corrupt-backup crash-loop path.
    """
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    key = str(db_path.resolve())
    kb._INITIALIZED_PATHS.discard(key)
    kb._LAST_INTEGRITY_PROBE.pop(key, None)

    real_reason = kb._integrity_check_reason
    calls: list[str] = []

    def synthetic_index_failure(conn, *, pragma="integrity_check"):
        calls.append(pragma)
        if len(calls) == 1:
            return (
                "integrity_check returned "
                "'row 15 missing from index idx_lane_heartbeats_state'"
            )
        return real_reason(conn, pragma=pragma)

    monkeypatch.setattr(kb, "_integrity_check_reason", synthetic_index_failure)

    conn = kb.connect(db_path=db_path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    assert calls == ["integrity_check", "integrity_check"]
    assert key in kb._INITIALIZED_PATHS
    assert key in kb._LAST_INTEGRITY_PROBE
    assert list(tmp_path.glob("kanban.db.corrupt.*")) == []


def test_non_index_integrity_corruption_still_fails_closed(tmp_path, monkeypatch):
    """The reindex path must be narrow: non-index corruption still backs up."""
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    key = str(db_path.resolve())
    kb._INITIALIZED_PATHS.discard(key)
    kb._LAST_INTEGRITY_PROBE.pop(key, None)

    monkeypatch.setattr(
        kb,
        "_integrity_check_reason",
        lambda conn, *, pragma="integrity_check": (
            "integrity_check returned 'database disk image is malformed'"
        ),
    )

    with pytest.raises(kb.KanbanDbCorruptError) as excinfo:
        kb.connect(db_path=db_path)

    assert "database disk image is malformed" in excinfo.value.reason
    assert excinfo.value.backup_path is not None
    assert excinfo.value.backup_path.exists()
    assert key not in kb._INITIALIZED_PATHS


def test_reprobe_not_run_within_default_interval(tmp_path, monkeypatch):
    """Within the (default 300s) interval a cached path opens exactly one
    sqlite connection — no probe connection on the hot path."""
    monkeypatch.delenv("HERMES_KANBAN_INTEGRITY_RECHECK_SECONDS", raising=False)
    db_path = _fresh_connected_db(tmp_path)

    calls: list[tuple] = []
    real_connect = sqlite3.connect

    def counting_connect(*args, **kwargs):
        calls.append(args)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(kb.sqlite3, "connect", counting_connect)
    conn = kb.connect(db_path=db_path)
    conn.close()
    assert len(calls) == 1, f"expected only the real connection, saw {calls!r}"


def test_reprobe_disabled_when_interval_nonpositive(tmp_path, monkeypatch):
    """<= 0 disables the periodic re-probe even when one would be due."""
    monkeypatch.setenv("HERMES_KANBAN_INTEGRITY_RECHECK_SECONDS", "0")
    db_path = _fresh_connected_db(tmp_path)
    key = str(db_path.resolve())
    # Force a probe to be "due" if the feature were enabled.
    kb._LAST_INTEGRITY_PROBE.pop(key, None)

    calls: list[tuple] = []
    real_connect = sqlite3.connect

    def counting_connect(*args, **kwargs):
        calls.append(args)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(kb.sqlite3, "connect", counting_connect)
    conn = kb.connect(db_path=db_path)
    conn.close()
    assert len(calls) == 1, "disabled re-probe must not open probe connections"


def test_reprobe_lock_contention_skips_probe_and_defers(tmp_path, monkeypatch):
    """Lock/busy during the re-probe must NOT fail the hot path, must NOT
    classify as corruption, and must defer the next probe a full interval."""
    monkeypatch.setenv("HERMES_KANBAN_INTEGRITY_RECHECK_SECONDS", "0.000001")
    db_path = _fresh_connected_db(tmp_path)
    key = str(db_path.resolve())
    time.sleep(0.01)  # make the probe due

    real_connect = sqlite3.connect
    state = {"probe_seen": False}

    def flaky_connect(*args, **kwargs):
        if not state["probe_seen"]:
            # First connection attempt is the re-probe — simulate a lock.
            state["probe_seen"] = True
            raise sqlite3.OperationalError("database is locked")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(kb.sqlite3, "connect", flaky_connect)

    conn = kb.connect(db_path=db_path)  # must NOT raise
    try:
        conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
    finally:
        conn.close()

    assert state["probe_seen"] is True
    # Path stays trusted; no spurious backup; probe deferred (timestamp set).
    assert key in kb._INITIALIZED_PATHS
    assert list(tmp_path.glob("*.corrupt.*")) == []
    assert key in kb._LAST_INTEGRITY_PROBE


def test_wal_without_shm_cache_hit_forces_rw_reverify(tmp_path, monkeypatch):
    """A cached WAL-without-``-shm`` path can't be re-probed read-only.

    Rather than keep silently trusting the cached verdict forever (the hole an
    adversarial review flagged), the guard must (a) create no sidecars,
    (b) drop the path from ``_INITIALIZED_PATHS``, and (c) return ``False`` so
    the caller read/write-verifies the live connection (integrity_check) on
    THIS connect, before any schema migration — not merely on a later one.
    """
    monkeypatch.setenv("HERMES_KANBAN_INTEGRITY_RECHECK_SECONDS", "0.000001")
    db_path = _copied_wal_db_without_shm(tmp_path)
    key = str(db_path.resolve())
    kb._INITIALIZED_PATHS.add(key)
    kb._LAST_INTEGRITY_PROBE.pop(key, None)

    result = kb._guard_existing_db_is_healthy(db_path)

    assert result is False  # forces the caller's read/write integrity_check now
    assert key not in kb._INITIALIZED_PATHS  # trusted-cache entry dropped
    assert not (tmp_path / "kanban.db-shm").exists()  # no sidecar created


def test_reprobe_connection_is_read_only(tmp_path, monkeypatch):
    """The periodic re-probe must open the DB via URI mode=ro so it can
    never checkpoint, recover, or take write locks on a WAL/hot-journal DB."""
    monkeypatch.setenv("HERMES_KANBAN_INTEGRITY_RECHECK_SECONDS", "0.000001")
    db_path = _fresh_connected_db(tmp_path)
    time.sleep(0.01)  # make the probe due

    calls: list[tuple[tuple, dict]] = []
    real_connect = sqlite3.connect

    def capturing_connect(*args, **kwargs):
        calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(kb.sqlite3, "connect", capturing_connect)
    conn = kb.connect(db_path=db_path)
    conn.close()

    # First connection is the re-probe, second is the real connection.
    assert len(calls) == 2, f"expected probe + real connection, saw {calls!r}"
    probe_args, probe_kwargs = calls[0]
    assert probe_args[0].startswith("file:")
    assert "mode=ro" in probe_args[0]
    assert probe_kwargs.get("uri") is True
    real_args, real_kwargs = calls[1]
    assert "mode=ro" not in str(real_args[0])

    # And the probe connection genuinely cannot write.
    ro = real_connect(probe_args[0], **probe_kwargs)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro.execute("CREATE TABLE probe_should_not_write(x)")
    finally:
        ro.close()


def test_periodic_reprobe_uses_full_integrity_check(tmp_path, monkeypatch):
    """The periodic re-probe must run ``PRAGMA integrity_check``, not
    ``quick_check`` — quick_check skips the index<->table consistency pass and
    would miss the orphan-index corruption class the re-probe exists to catch.
    """
    monkeypatch.setenv("HERMES_KANBAN_INTEGRITY_RECHECK_SECONDS", "0.000001")
    db_path = _fresh_connected_db(tmp_path)
    time.sleep(0.01)  # make the probe due

    real_reason = kb._integrity_check_reason
    pragmas: list[str] = []

    def spy(conn, *, pragma="integrity_check"):
        pragmas.append(pragma)
        return real_reason(conn, pragma=pragma)

    monkeypatch.setattr(kb, "_integrity_check_reason", spy)
    conn = kb.connect(db_path=db_path)
    conn.close()

    # The cached-healthy second connect fires exactly the periodic re-probe,
    # which must use the full integrity_check (never quick_check).
    assert pragmas == ["integrity_check"], f"re-probe pragmas: {pragmas!r}"
    assert "quick_check" not in pragmas


def test_nested_write_txn_still_fails_fast(tmp_path):
    db_path = _fresh_connected_db(tmp_path)

    with kb.connect(db_path=db_path) as conn:
        with pytest.raises(sqlite3.OperationalError, match="transaction"):
            with kb.write_txn(conn):
                with kb.write_txn(conn):
                    conn.execute("SELECT 1")


def test_write_forensics_opt_in_records_txn_and_continuity_events(
    tmp_path, monkeypatch
):
    db_path = _fresh_connected_db(tmp_path)
    out_dir = tmp_path / "write-forensics"
    monkeypatch.setenv("HERMES_KANBAN_WRITE_FORENSICS", "1")
    monkeypatch.setenv("HERMES_KANBAN_WRITE_FORENSICS_DIR", str(out_dir))

    with kb.connect(db_path=db_path) as conn:
        kb.write_lane_continuity_packet(
            conn,
            lane_id="dev-a",
            now=100,
            packet={
                "current_objective": "trace writes",
                "current_repo_branch_pr": "repo/main#1",
                "files_touched_or_planned": [],
                "active_blocker": None,
                "last_verified_command_check": "pytest",
                "next_safe_action": "inspect log",
                "explicit_non_claims": ["not merged"],
                "operator_approvals_relied_on": [],
            },
        )

    log_files = list(out_dir.glob("kanban-writes-*.jsonl"))
    assert log_files
    events = [
        json.loads(line)
        for line in log_files[0].read_text().splitlines()
        if line.strip()
    ]
    assert any(
        event["action"] == "write_lane_continuity_packet"
        and event["phase"] == "write"
        and event["lane_id"] == "dev-a"
        and event.get("packet_sha256")
        for event in events
    )
    assert any(
        event["action"] == "write_txn" and event["phase"] == "begin"
        for event in events
    )
    assert any(
        event["action"] == "write_txn" and event["phase"] == "commit"
        for event in events
    )


def test_write_forensics_io_failure_does_not_block_commit(tmp_path, monkeypatch):
    db_path = _fresh_connected_db(tmp_path)
    not_a_dir = tmp_path / "not-a-dir"
    not_a_dir.write_text("occupied")
    monkeypatch.setenv("HERMES_KANBAN_WRITE_FORENSICS", "1")
    monkeypatch.setenv("HERMES_KANBAN_WRITE_FORENSICS_DIR", str(not_a_dir))

    with kb.connect(db_path=db_path) as conn:
        kb.write_lane_continuity_packet(
            conn,
            lane_id="dev-a",
            now=100,
            packet={
                "current_objective": "trace writes",
                "current_repo_branch_pr": "repo/main#1",
                "files_touched_or_planned": [],
                "active_blocker": None,
                "last_verified_command_check": "pytest",
                "next_safe_action": "inspect log",
                "explicit_non_claims": ["not merged"],
                "operator_approvals_relied_on": [],
            },
        )
        row = conn.execute(
            "SELECT packet_json FROM lane_continuity_packets WHERE lane_id='dev-a'"
        ).fetchone()

    assert row is not None
    assert "trace writes" in row["packet_json"]


def test_write_forensics_size_cap_skips_append(tmp_path, monkeypatch):
    db_path = _fresh_connected_db(tmp_path)
    out_dir = tmp_path / "write-forensics"
    out_dir.mkdir()
    log_path = out_dir / f"kanban-writes-{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    log_path.write_text("x", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_WRITE_FORENSICS", "1")
    monkeypatch.setenv("HERMES_KANBAN_WRITE_FORENSICS_DIR", str(out_dir))
    monkeypatch.setenv("HERMES_KANBAN_WRITE_FORENSICS_MAX_BYTES", "1")

    with kb.connect(db_path=db_path) as conn:
        kb.write_lane_continuity_packet(
            conn,
            lane_id="dev-a",
            now=100,
            packet={
                "current_objective": "trace writes",
                "current_repo_branch_pr": "repo/main#1",
                "files_touched_or_planned": [],
                "active_blocker": None,
                "last_verified_command_check": "pytest",
                "next_safe_action": "inspect log",
                "explicit_non_claims": ["not merged"],
                "operator_approvals_relied_on": [],
            },
        )

    assert log_path.read_text(encoding="utf-8") == "x"


def test_reprobe_timestamp_set_before_probe_coalesces(tmp_path, monkeypatch):
    """The probe timestamp is recorded BEFORE the probe connection opens, so
    concurrent connects skip instead of stacking probes behind the lock."""
    monkeypatch.setenv("HERMES_KANBAN_INTEGRITY_RECHECK_SECONDS", "0.000001")
    db_path = _fresh_connected_db(tmp_path)
    key = str(db_path.resolve())
    time.sleep(0.01)  # make the probe due
    kb._LAST_INTEGRITY_PROBE.pop(key, None)

    seen: dict[str, object] = {}
    real_connect = sqlite3.connect

    def observing_connect(*args, **kwargs):
        seen.setdefault("stamp_at_probe", kb._LAST_INTEGRITY_PROBE.get(key))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(kb.sqlite3, "connect", observing_connect)
    conn = kb.connect(db_path=db_path)
    conn.close()

    assert seen["stamp_at_probe"] is not None, (
        "timestamp must be stamped before the probe connection opens"
    )


def test_reprobe_effective_interval_jitter_bounded_and_deterministic():
    """Per-process jitter: within (interval, interval*1.1], stable across
    calls in the same process, and derived from os.getpid()."""
    import os

    interval = 300.0
    first = kb._reprobe_effective_interval(interval)
    second = kb._reprobe_effective_interval(interval)
    assert first == second, "jitter must be deterministic within a process"
    assert interval <= first <= interval * 1.1
    expected = interval * (1.0 + 0.1 * ((os.getpid() % 1024) / 1024.0))
    assert first == expected


def test_integrity_recheck_seconds_env_parsing(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_INTEGRITY_RECHECK_SECONDS", raising=False)
    assert kb._integrity_recheck_seconds() == 300.0
    monkeypatch.setenv("HERMES_KANBAN_INTEGRITY_RECHECK_SECONDS", "42.5")
    assert kb._integrity_recheck_seconds() == 42.5
    monkeypatch.setenv("HERMES_KANBAN_INTEGRITY_RECHECK_SECONDS", "not-a-number")
    assert kb._integrity_recheck_seconds() == 300.0


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_integrity_recheck_seconds_nonfinite_falls_back(monkeypatch, raw):
    """float() accepts NaN/inf; both must fall back to the default instead
    of producing an interval that fails comparisons unpredictably."""
    monkeypatch.setenv("HERMES_KANBAN_INTEGRITY_RECHECK_SECONDS", raw)
    assert kb._integrity_recheck_seconds() == 300.0


# ---------------------------------------------------------------------------
# .corrupt backup swarm cap
# ---------------------------------------------------------------------------

def _make_fake_corrupt_backups(db_path: Path, count: int) -> None:
    for i in range(count):
        (db_path.parent / f"{db_path.name}.corrupt.fake{i}.bak").write_bytes(b"x")


def test_backup_cap_skips_new_backup_at_cap(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HERMES_KANBAN_CORRUPT_BACKUP_CAP", "3")
    db_path = tmp_path / "kanban.db"
    original = _write_corrupt_db(db_path)
    _make_fake_corrupt_backups(db_path, 3)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        with pytest.raises(kb.KanbanDbCorruptError) as excinfo:
            kb.connect(db_path=db_path)

    # Error still raised loudly, but no new backup was written.
    assert excinfo.value.backup_path is None
    siblings = sorted(p.name for p in tmp_path.glob("kanban.db.corrupt.*"))
    assert siblings == [f"kanban.db.corrupt.fake{i}.bak" for i in range(3)]
    # Original bytes untouched.
    assert db_path.read_bytes() == original
    # One warning via the module logger.
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "corrupt-backup cap" in r.getMessage()
    ]
    assert len(warnings) == 1


def test_backup_cap_under_cap_still_creates_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_CORRUPT_BACKUP_CAP", "3")
    db_path = tmp_path / "kanban.db"
    original = _write_corrupt_db(db_path)
    _make_fake_corrupt_backups(db_path, 2)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    with pytest.raises(kb.KanbanDbCorruptError) as excinfo:
        kb.connect(db_path=db_path)

    backup = excinfo.value.backup_path
    assert backup is not None and backup.exists()
    assert backup.read_bytes() == original


def test_backup_cap_counts_literally_with_glob_metachar_filename(
    tmp_path, monkeypatch
):
    """A DB filename containing glob metacharacters ("kanban[1].db") must
    still count its .corrupt.* siblings — parent.glob() would treat "[1]"
    as a character class, count 0, and defeat the cap."""
    monkeypatch.setenv("HERMES_KANBAN_CORRUPT_BACKUP_CAP", "3")
    db_path = tmp_path / "kanban[1].db"
    original = _write_corrupt_db(db_path)
    for i in range(3):
        (tmp_path / f"kanban[1].db.corrupt.fake{i}.bak").write_bytes(b"x")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    with pytest.raises(kb.KanbanDbCorruptError) as excinfo:
        kb.connect(db_path=db_path)

    # Cap honored: no new backup despite the metachar name.
    assert excinfo.value.backup_path is None
    siblings = sorted(
        p.name for p in tmp_path.iterdir()
        if p.name.startswith("kanban[1].db.corrupt.")
    )
    assert siblings == [f"kanban[1].db.corrupt.fake{i}.bak" for i in range(3)]
    assert db_path.read_bytes() == original


def test_corrupt_backup_cap_env_parsing(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_CORRUPT_BACKUP_CAP", raising=False)
    assert kb._corrupt_backup_cap() == 16
    monkeypatch.setenv("HERMES_KANBAN_CORRUPT_BACKUP_CAP", "5")
    assert kb._corrupt_backup_cap() == 5
    monkeypatch.setenv("HERMES_KANBAN_CORRUPT_BACKUP_CAP", "garbage")
    assert kb._corrupt_backup_cap() == 16


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "1e2", "16.0"])
def test_corrupt_backup_cap_nonfinite_and_float_strings_fall_back(
    monkeypatch, raw
):
    """Non-finite and float-shaped strings must land on the default cap,
    never a NaN/inf that breaks the >= cap comparison."""
    monkeypatch.setenv("HERMES_KANBAN_CORRUPT_BACKUP_CAP", raw)
    assert kb._corrupt_backup_cap() == 16
