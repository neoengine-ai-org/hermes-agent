"""Tests for the cross-process kanban write lock (hermes_cli.kanban_db).

The lock is the primitive that makes `hermes kanban swap` safe: writers take
`<db>.write.lock` around every `write_txn`, and the offline swap takes it
exclusively so no cooperating writer is mid-transaction during the inode swap.

Key invariants proven here:
  * reentrant within a thread — nested `write_txn` must fail fast, never
    self-deadlock (an earlier non-reentrant version deadlocked and was reverted);
  * serializing across threads/processes — a second holder blocks / times out;
  * opt-out via HERMES_KANBAN_WRITE_LOCK with SQLite's own serialization intact.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def _file_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=5)
    conn.execute("CREATE TABLE IF NOT EXISTS counter(n INTEGER)")
    conn.execute("INSERT INTO counter(n) VALUES (0)")
    conn.commit()
    return conn


@pytest.fixture(autouse=True)
def _write_lock_enabled(monkeypatch):
    # Default-on regardless of the ambient environment the suite runs in.
    monkeypatch.delenv("HERMES_KANBAN_WRITE_LOCK", raising=False)
    # Reset per-thread reentrancy state so tests don't leak depth into each
    # other (the module keeps a threading.local dict).
    if hasattr(kb._write_lock_local, "depths"):
        kb._write_lock_local.depths.clear()
    yield


def test_write_lock_file_created_when_enabled(tmp_path):
    db = tmp_path / "board.db"
    conn = _file_conn(db)
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE counter SET n = n + 1")
    finally:
        conn.close()
    assert kb._kanban_write_lock_path(db).exists(), \
        "write_txn must create/hold the <db>.write.lock sidecar when enabled"


def test_write_lock_path_canonical_through_symlink(tmp_path):
    """Symlinked and resolved DB paths must map to the SAME lock file, or two
    processes would take different locks and lose mutual exclusion."""
    real = tmp_path / "real.db"
    real.write_bytes(b"x")
    link = tmp_path / "link.db"
    link.symlink_to(real)
    assert kb._kanban_write_lock_path(link) == kb._kanban_write_lock_path(real)
    assert kb._kanban_write_lock_path(link).name == "real.db.write.lock"
    # init lock (the connect-gate swap also holds) canonicalizes the same way.
    assert kb._kanban_lock_sidecar(link, "init.lock") == \
        kb._kanban_lock_sidecar(real, "init.lock")


def test_write_lock_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_WRITE_LOCK", "0")
    db = tmp_path / "board.db"
    conn = _file_conn(db)
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE counter SET n = n + 1")
        row = conn.execute("SELECT n FROM counter").fetchone()
    finally:
        conn.close()
    assert row[0] == 1, "writes must still commit with the lock disabled"
    assert not kb._kanban_write_lock_path(db).exists(), \
        "disabled write lock must not create the sidecar lock file"


def test_nested_write_txn_fails_fast_not_deadlock(tmp_path):
    """Nested write_txn must raise (SQLite 'transaction within a transaction'),
    NOT hang — the exact regression a non-reentrant flock would reintroduce."""
    db = tmp_path / "board.db"
    _file_conn(db).close()
    outcome: dict[str, object] = {}

    def _run():
        # Connection must be created in this thread (sqlite3 objects are
        # thread-affine); the point of the test is the reentrant flock.
        conn = sqlite3.connect(str(db), timeout=5)
        try:
            with kb.write_txn(conn):
                with kb.write_txn(conn):  # nested — must fail fast
                    pass
        except sqlite3.OperationalError as exc:
            outcome["error"] = str(exc)
        except BaseException as exc:  # pragma: no cover - defensive
            outcome["error"] = f"{type(exc).__name__}: {exc}"
        else:
            outcome["error"] = None
        finally:
            conn.close()

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout=5.0)
    assert not worker.is_alive(), "nested write_txn deadlocked (write lock not reentrant)"
    assert outcome.get("error") is not None, "nested write_txn should have raised"
    assert "transaction" in str(outcome["error"]).lower()


def test_concurrent_threads_no_lost_updates(tmp_path):
    """Two threads (separate connections, separate flock fds) hammering
    write_txn must serialize cleanly — no lost updates, no deadlock."""
    db = tmp_path / "board.db"
    seed = _file_conn(db)
    seed.close()
    iters = 50

    def _bump():
        conn = sqlite3.connect(str(db), timeout=10)
        try:
            for _ in range(iters):
                with kb.write_txn(conn):
                    conn.execute("UPDATE counter SET n = n + 1")
        finally:
            conn.close()

    threads = [threading.Thread(target=_bump) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "writer thread hung under the write lock"

    conn = sqlite3.connect(str(db))
    try:
        total = conn.execute("SELECT n FROM counter").fetchone()[0]
    finally:
        conn.close()
    assert total == 2 * iters, f"expected {2 * iters} increments, got {total}"


def test_exclusive_maintenance_lock_times_out_when_held(tmp_path):
    """A second exclusive acquisition (different fd) must time out while the
    first is held — this is what makes `swap` refuse while a process is still
    attaching to the DB (the init lock is also the connect() gate)."""
    db = tmp_path / "board.db"
    _file_conn(db).close()

    held = threading.Event()
    release = threading.Event()

    def _holder():
        with kb.exclusive_maintenance_lock(db, timeout=5.0):
            held.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=_holder, daemon=True)
    holder.start()
    assert held.wait(timeout=5), "holder never acquired the lock"

    if kb.fcntl is None:  # pragma: no cover - non-POSIX
        release.set()
        holder.join(timeout=5)
        pytest.skip("fcntl unavailable; exclusive lock is a no-op")

    with pytest.raises(kb.KanbanMaintenanceLockTimeout):
        with kb.exclusive_maintenance_lock(db, timeout=0.5):
            pass

    release.set()
    holder.join(timeout=5)
    # Once released it must be acquirable again.
    with kb.exclusive_maintenance_lock(db, timeout=2.0):
        pass


def test_maintenance_lock_blocks_connect_gate(tmp_path):
    """The maintenance lock is the SAME lock connect() takes around its open, so
    holding it must block a concurrent connect() until released — the guarantee
    that no writer attaches during a swap."""
    db = tmp_path / "board.db"
    kb.connect(db).close()  # first-connect init done
    if kb.fcntl is None:  # pragma: no cover - non-POSIX
        pytest.skip("fcntl unavailable; locks are no-ops")

    opened = threading.Event()
    proceed = threading.Event()

    def _connector():
        proceed.wait(timeout=10)
        c = kb.connect(db)
        c.close()
        opened.set()

    with kb.exclusive_maintenance_lock(db, timeout=5.0):
        worker = threading.Thread(target=_connector, daemon=True)
        worker.start()
        proceed.set()
        # connect() must NOT complete while we hold the maintenance lock.
        assert not opened.wait(timeout=1.0), "connect() ran while maintenance lock held"
    # Released → the blocked connect() now completes.
    assert opened.wait(timeout=5), "connect() never completed after lock release"
    worker.join(timeout=5)
