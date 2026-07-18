"""Tests for the bounded kanban init lock (issue #36644).

`connect()` wrapped its entire body in an unbounded blocking `flock(LOCK_EX)`
on every call. A single process stalled inside the critical section blocked the
long-lived gateway dispatcher's next-tick `connect()` forever — no timeout, no
recovery, board silently stops being worked.

Two fixes, both covered here:
1. Fast path: once initialized, connects take a concurrent shared attach lock.
2. Bounded acquire: attach/init fails closed on timeout, so offline maintenance
   can never be bypassed by a connect that waited longer than the deadline.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    return home


def _hold_init_lock(db_path: Path):
    """Return (start_event, release_event, thread) holding the init lock."""
    holding = threading.Event()
    release = threading.Event()

    def _holder():
        with kb._cross_process_init_lock(db_path):
            holding.set()
            release.wait(timeout=10)

    t = threading.Thread(target=_holder, daemon=True)
    t.start()
    assert holding.wait(timeout=5), "holder thread never acquired the lock"
    return release, t


def test_initialized_path_connects_share_attach_lock(kanban_home):
    """Steady-state connects must not serialize behind peer readers."""
    db_path = kb.kanban_db_path(board="default")
    # Initialize once.
    kb.connect().close()
    assert str(db_path.resolve()) in kb._INITIALIZED_PATHS

    holding = threading.Event()
    release = threading.Event()

    def _reader():
        with kb._cross_process_attach_lock(db_path):
            holding.set()
            release.wait(timeout=10)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    assert holding.wait(timeout=5)
    try:
        start = time.monotonic()
        kb.connect().close()
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"fast-path connect blocked on the init lock ({elapsed:.2f}s)"
    finally:
        release.set()
        t.join(timeout=5)


def test_first_init_connect_fails_closed_when_lock_held(kanban_home, monkeypatch):
    """First-init connect must time out rather than bypass maintenance."""
    monkeypatch.setattr(kb, "_INIT_LOCK_TIMEOUT_SECONDS", 0.6)
    db_path = kb.kanban_db_path(board="default")

    release, t = _hold_init_lock(db_path)
    try:
        start = time.monotonic()
        with pytest.raises(kb.KanbanInitLockTimeout):
            kb.connect()
        elapsed = time.monotonic() - start
        # Failed within roughly the timeout window (not unbounded).
        assert 0.4 <= elapsed < 3.0, f"expected bounded ~0.6s acquire, got {elapsed:.2f}s"
        assert str(db_path.resolve()) not in kb._INITIALIZED_PATHS
    finally:
        release.set()
        t.join(timeout=5)


@pytest.mark.skipif(kb.fcntl is None, reason="POSIX flock required")
def test_initialized_connect_fails_closed_during_exclusive_maintenance(
    kanban_home, monkeypatch,
):
    """A cached fast-path connect must not bypass an offline DB swap."""
    monkeypatch.setattr(kb, "_INIT_LOCK_TIMEOUT_SECONDS", 0.6)
    db_path = kb.kanban_db_path(board="default")
    kb.connect().close()
    assert str(db_path.resolve()) in kb._INITIALIZED_PATHS

    holding = threading.Event()
    release = threading.Event()

    def _maintenance():
        with kb.exclusive_maintenance_lock(db_path):
            holding.set()
            release.wait(timeout=10)

    thread = threading.Thread(target=_maintenance, daemon=True)
    thread.start()
    assert holding.wait(timeout=5)
    try:
        start = time.monotonic()
        with pytest.raises(kb.KanbanInitLockTimeout):
            kb.connect()
        elapsed = time.monotonic() - start
        assert 0.4 <= elapsed < 3.0
    finally:
        release.set()
        thread.join(timeout=5)
