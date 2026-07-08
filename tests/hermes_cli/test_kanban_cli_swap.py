"""Tests for `hermes kanban swap` — the lock-protected, quiescence-verified
atomic swap of a recovered kanban DB into the live path (hermes_cli.kanban).

`recover` deliberately never swaps; doing that swap by hand while writers held
open fds is the confirmed root cause of the repeated page corruption. `swap`
enforces the quiesce-first discipline: exclusive write lock + lsof holder check
+ incoming integrity gate + atomic os.replace + backup + post-swap re-verify
with restore-on-failure.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_kanban(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args(["kanban"] + argv)
    return kc.kanban_command(args)


def _make_healthy_db(path: Path, rows: int = 25, marker: str | None = None) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, label TEXT)")
        conn.executemany(
            "INSERT INTO items(label) VALUES (?)",
            [(f"item-{i}",) for i in range(rows)],
        )
        if marker:
            conn.execute(f"CREATE TABLE {marker}(x TEXT)")
            conn.execute(f"INSERT INTO {marker}(x) VALUES ('present')")
        conn.commit()
    finally:
        conn.close()


def _write_corrupt_db(path: Path) -> bytes:
    header = b"SQLite format 3\x00" + b"\x10\x00\x02\x02\x00\x40\x20\x20"
    header += b"\x00\x00\x00\x0c\x00\x00\x23\x46\x00\x00\x00\x00"
    header = header.ljust(100, b"\x00")
    payload = b"definitely not a valid sqlite page \x00\x01\x02\x03" * 64
    blob = header + payload
    path.write_bytes(blob)
    return blob


def _clobber_middle_page(path: Path, page_size: int = 4096) -> None:
    data = bytearray(path.read_bytes())
    start = page_size * 2
    assert len(data) > start + page_size, "db too small to clobber safely"
    data[start:start + page_size] = b"\xde\xad" * (page_size // 2)
    path.write_bytes(bytes(data))


def _integrity_ok(path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        # A sufficiently malformed image makes integrity_check itself raise.
        return False


def _sqlite3_cli_supports_recover() -> bool:
    binary = shutil.which("sqlite3")
    if not binary:
        return False
    try:
        proc = subprocess.run([binary, ":memory:", ".recover"],
                              capture_output=True, text=True, timeout=30)
    except Exception:
        return False
    return proc.returncode == 0 and "unknown command" not in (proc.stderr or "").lower()


requires_recover = pytest.mark.skipif(
    not _sqlite3_cli_supports_recover(),
    reason="sqlite3 CLI missing or lacks .recover support",
)
requires_lsof = pytest.mark.skipif(
    shutil.which("lsof") is None, reason="lsof unavailable"
)

_HOLDER = (
    "import sys,time\n"
    "f=open(sys.argv[1],'rb')\n"
    "open(sys.argv[2],'w').write('ready')\n"
    "time.sleep(float(sys.argv[3]))\n"
)


class _Holder:
    """A separate process holding the DB file open (so lsof reports it)."""

    def __init__(self, db: Path, tmp: Path, seconds: float = 30):
        self.ready = tmp / "holder.ready"
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(db), str(self.ready), str(seconds)]
        )

    def wait_ready(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ready.exists():
                return True
            time.sleep(0.05)
        return False

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


# ---------------------------------------------------------------------------
# usage / argument validation
# ---------------------------------------------------------------------------

def test_swap_requires_a_source(tmp_path, capsys):
    db = tmp_path / "k.db"
    _make_healthy_db(db)
    rc = _run_kanban(["swap", "--db", str(db)])
    assert rc == 2
    assert "choose one of" in capsys.readouterr().err
    assert db.exists()


def test_swap_missing_live_db(tmp_path, capsys):
    missing = tmp_path / "nope.db"
    src = tmp_path / "src.db"
    _make_healthy_db(src)
    rc = _run_kanban(["swap", "--db", str(missing), "--from", str(src)])
    assert rc == 4
    assert "no such live database" in capsys.readouterr().err


def test_swap_missing_from(tmp_path, capsys):
    db = tmp_path / "k.db"
    _make_healthy_db(db)
    rc = _run_kanban(["swap", "--db", str(db), "--from", str(tmp_path / "ghost.db")])
    assert rc == 4
    assert "does not exist" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------

def test_swap_from_healthy_roundtrip(tmp_path, capsys):
    db = tmp_path / "k.db"
    _make_healthy_db(db, rows=25)
    original = db.read_bytes()
    recovered = tmp_path / "recovered.db"
    _make_healthy_db(recovered, rows=10, marker="swapped")

    rc = _run_kanban(["swap", "--db", str(db), "--from", str(recovered)])
    out = capsys.readouterr()
    payload = json.loads(out.out)

    assert rc == 0, out.err
    assert payload["verdict"] == "ok"
    assert payload["quiescence"]["verified"] is True
    assert payload["quiescence"]["holders"] == []
    assert payload["post_swap_integrity"].startswith("quick_check=ok")

    # Live DB now holds the recovered content.
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 10
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='swapped'"
        ).fetchone()[0] == 1
    finally:
        conn.close()

    # The old DB was preserved as a pre-swap backup.
    backups = list(tmp_path.glob("k.db.pre-swap.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert "Swap complete" in out.err


def test_swap_dry_run_reports_quiescence_without_swapping(tmp_path, capsys):
    db = tmp_path / "k.db"
    _make_healthy_db(db, rows=7)
    before = db.read_bytes()

    rc = _run_kanban(["swap", "--db", str(db), "--dry-run"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["verdict"] == "dry-run-ok"
    assert payload["quiescence"]["verified"] is True
    assert db.read_bytes() == before, "dry-run must not modify the live DB"
    assert list(tmp_path.glob("*.pre-swap.*")) == []


@requires_recover
def test_swap_recover_inline(tmp_path, capsys):
    db = tmp_path / "k.db"
    _make_healthy_db(db, rows=2000)  # >3 pages so an interior page can be clobbered
    _clobber_middle_page(db)
    corrupt_bytes = db.read_bytes()
    assert not _integrity_ok(db)

    rc = _run_kanban(["swap", "--db", str(db), "--recover"])
    out = capsys.readouterr()
    payload = json.loads(out.out)

    assert rc == 0, out.err
    assert payload["verdict"] == "ok"
    assert payload["recover"]["verdict"] == "ok"
    assert _integrity_ok(db), "live DB must be clean after an inline recover-swap"

    backups = list(tmp_path.glob("k.db.pre-swap.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == corrupt_bytes


# ---------------------------------------------------------------------------
# fail-closed guards
# ---------------------------------------------------------------------------

def test_swap_refuses_corrupt_incoming(tmp_path, capsys):
    db = tmp_path / "k.db"
    _make_healthy_db(db, rows=12)
    before = db.read_bytes()
    corrupt = tmp_path / "bad.db"
    _write_corrupt_db(corrupt)

    rc = _run_kanban(["swap", "--db", str(db), "--from", str(corrupt)])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 3
    assert payload["verdict"] == "incoming-corrupt"
    assert db.read_bytes() == before, "must not swap in a corrupt DB"
    assert list(tmp_path.glob("*.pre-swap.*")) == [], "no backup on a refused swap"


def test_swap_backup_dir(tmp_path, capsys):
    db = tmp_path / "k.db"
    _make_healthy_db(db, rows=5)
    recovered = tmp_path / "recovered.db"
    _make_healthy_db(recovered, rows=5)
    bdir = tmp_path / "backups"

    rc = _run_kanban(["swap", "--db", str(db), "--from", str(recovered),
                      "--backup-dir", str(bdir)])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert Path(payload["backup"]).parent == bdir
    assert len(list(bdir.glob("k.db.pre-swap.*.bak"))) == 1


@requires_lsof
def test_swap_refuses_while_writer_attached(tmp_path, capsys):
    db = tmp_path / "k.db"
    _make_healthy_db(db, rows=9)
    before = db.read_bytes()
    recovered = tmp_path / "recovered.db"
    _make_healthy_db(recovered, rows=9)

    holder = _Holder(db, tmp_path)
    try:
        assert holder.wait_ready(), "holder process never opened the DB"
        rc = _run_kanban(["swap", "--db", str(db), "--from", str(recovered)])
    finally:
        holder.stop()
    payload = json.loads(capsys.readouterr().out)

    assert rc == 5, "swap must refuse (exit 5) while another process holds the DB open"
    assert payload["quiescence"]["holders"], "the attached process must be reported"
    assert db.read_bytes() == before, "must not swap while writers are attached"
    assert list(tmp_path.glob("*.pre-swap.*")) == []


def test_swap_moves_stale_sidecars_off_live_path(tmp_path, capsys):
    """Stale -wal/-shm must be moved aside BEFORE the new inode is exposed, and
    preserved with the backup — a new DB sharing old sidecars re-corrupts."""
    db = tmp_path / "k.db"
    _make_healthy_db(db, rows=8)
    (tmp_path / "k.db-wal").write_bytes(b"stale-wal-frames")
    (tmp_path / "k.db-shm").write_bytes(b"stale-shm")
    recovered = tmp_path / "recovered.db"
    _make_healthy_db(recovered, rows=8)

    rc = _run_kanban(["swap", "--db", str(db), "--from", str(recovered)])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    # Live path is clean: no stale sidecars beside the new DB.
    assert not (tmp_path / "k.db-wal").exists()
    assert not (tmp_path / "k.db-shm").exists()
    assert _integrity_ok(db)
    # Sidecars preserved alongside the backup.
    bak = Path(payload["backup"])
    assert (bak.parent / (bak.name + "-wal")).exists()
    assert (bak.parent / (bak.name + "-shm")).exists()
    assert sorted(payload["moved_sidecars"]) == sorted(
        [str(bak) + "-wal", str(bak) + "-shm"])


def test_swap_fails_closed_on_lsof_error(tmp_path, capsys, monkeypatch):
    """A real lsof failure (nonzero + stderr) must fail closed, not read as
    'quiesced'. Only lsof runs as a subprocess in --from mode, so patching
    subprocess.run here is safe."""
    db = tmp_path / "k.db"
    _make_healthy_db(db, rows=5)
    before = db.read_bytes()
    recovered = tmp_path / "r.db"
    _make_healthy_db(recovered, rows=5)

    class _FakeProc:
        returncode = 1
        stdout = ""
        stderr = "lsof: WARNING: can't stat() /proc: Operation not permitted"

    monkeypatch.setattr(kc.subprocess, "run", lambda *a, **k: _FakeProc())
    rc = _run_kanban(["swap", "--db", str(db), "--from", str(recovered)])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 5, "lsof error must fail closed (writers-active), not proceed"
    assert payload["quiescence"]["verified"] is False
    assert "lsof" in payload["quiescence"]["error"]
    assert db.read_bytes() == before, "no swap on unverifiable quiescence"


def test_swap_force_does_not_bypass_maintenance_lock(tmp_path, capsys):
    """--force overrides only the lsof holder check, NEVER the maintenance lock:
    proceeding unlocked would reopen the connect-attach race the design closes."""
    if kc.kb.fcntl is None:
        pytest.skip("fcntl unavailable; locks are no-ops")
    db = tmp_path / "k.db"
    _make_healthy_db(db, rows=5)
    before = db.read_bytes()
    recovered = tmp_path / "r.db"
    _make_healthy_db(recovered, rows=5)

    import threading
    held = threading.Event()
    release = threading.Event()

    def _holder():
        with kc.kb.exclusive_maintenance_lock(db, timeout=5.0):
            held.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=_holder, daemon=True)
    holder.start()
    assert held.wait(timeout=5), "holder never took the maintenance lock"
    try:
        rc = _run_kanban(["swap", "--db", str(db), "--from", str(recovered),
                          "--force", "--lock-timeout", "0.5"])
    finally:
        release.set()
        holder.join(timeout=5)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 5, "--force must NOT bypass the maintenance lock"
    assert payload["maintenance_lock"] == "timeout"
    assert db.read_bytes() == before, "no swap while the maintenance lock is held"


def test_swap_fails_closed_when_maintenance_lock_unavailable(tmp_path, capsys, monkeypatch):
    """If the maintenance lock cannot actually be held (no fcntl), the swap must
    fail closed — never silently mutate the DB with no connect-gate."""
    import contextlib as _cl
    db = tmp_path / "k.db"
    _make_healthy_db(db, rows=5)
    before = db.read_bytes()
    recovered = tmp_path / "r.db"
    _make_healthy_db(recovered, rows=5)

    @_cl.contextmanager
    def _no_lock(*a, **k):
        yield None  # simulate an fcntl-unavailable platform

    monkeypatch.setattr(kc.kb, "exclusive_maintenance_lock", _no_lock)
    rc = _run_kanban(["swap", "--db", str(db), "--from", str(recovered)])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 5
    assert payload["maintenance_lock"] == "unavailable(no-fcntl)"
    assert payload["verdict"] == "maintenance-lock-unavailable"
    assert db.read_bytes() == before, "must not swap without a real maintenance lock"


def test_swap_atomically_restores_on_post_swap_corruption(tmp_path, capsys, monkeypatch):
    """If the newly-swapped DB fails post-swap integrity, the original must be
    restored atomically (never half-written) and reported as restored."""
    db = tmp_path / "k.db"
    _make_healthy_db(db, rows=6, marker="orig")
    original = db.read_bytes()
    recovered = tmp_path / "r.db"
    _make_healthy_db(recovered, rows=2, marker="newnew")

    real_probe = kc._probe_integrity
    calls = {"n": 0}

    def fake_probe(path):
        calls["n"] += 1
        if calls["n"] >= 2:  # 1st = incoming (real, ok); 2nd = post-swap (forced bad)
            return "corrupt", "forced post-swap corruption"
        return real_probe(path)

    monkeypatch.setattr(kc, "_probe_integrity", fake_probe)
    rc = _run_kanban(["swap", "--db", str(db), "--from", str(recovered)])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 3
    assert payload["verdict"] == "post-swap-corrupt-restored"
    assert db.read_bytes() == original, "live DB must be atomically restored to the original"
    # And it's a usable DB with the original schema.
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='orig'").fetchone()[0] == 1
    finally:
        conn.close()


@requires_lsof
def test_swap_force_overrides_attached_writer(tmp_path, capsys):
    db = tmp_path / "k.db"
    _make_healthy_db(db, rows=9)
    recovered = tmp_path / "recovered.db"
    _make_healthy_db(recovered, rows=3, marker="forced")

    holder = _Holder(db, tmp_path)
    try:
        assert holder.wait_ready(), "holder process never opened the DB"
        rc = _run_kanban(["swap", "--db", str(db), "--from", str(recovered), "--force"])
    finally:
        holder.stop()
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0, "--force must proceed despite the attached writer"
    assert payload["verdict"] == "ok"
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 3
    finally:
        conn.close()
