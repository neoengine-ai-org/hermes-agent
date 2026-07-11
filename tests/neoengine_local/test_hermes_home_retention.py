"""Tests for the hermes home retention & rotation tool."""

import gzip
import json
import os
import sqlite3
import tarfile
import time

import pytest

from neoengine_local.hermes_home_retention import (
    CLASS_BULK,
    CLASS_RECEIPT,
    RetentionConfig,
    lane_archive_gc,
    lane_cron_output,
    lane_dispatch_logs,
    lane_lane_workdirs,
    lane_profile_caches,
    lane_state_db,
    main,
    run_retention,
)

DAY = 86400.0
OLD = time.time() - 30 * DAY
FRESH = time.time() - 60.0


def _touch(path, mtime, content=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))
    return path


def _cfg(root, execute=False, **overrides):
    root.mkdir(parents=True, exist_ok=True)
    return RetentionConfig(root=root, execute=execute, **overrides)


@pytest.fixture(autouse=True)
def _clear_kill_switches(monkeypatch):
    for key in list(os.environ):
        if key.startswith("HERMES_HOME_RETENTION"):
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# dispatch-logs
# ---------------------------------------------------------------------------

def test_dispatch_logs_age_and_keep_min(tmp_path):
    root = tmp_path / "hermes"
    logs = root / "state" / "nw-agent-dispatch" / "logs"
    old_files = [
        _touch(logs / f"lane-a-{i}.log", OLD - i) for i in range(30)
    ]
    fresh = _touch(logs / "lane-b-now.log", FRESH)
    cfg = _cfg(root, keep_min=25)
    report = lane_dispatch_logs(cfg, root)
    # 31 files, newest 25 survive, remaining 6 are all old => candidates
    assert report.candidate_files == 6
    assert fresh.exists()
    assert all(p.exists() for p in old_files)  # dry-run mutates nothing


def test_dispatch_logs_keep_min_protects_old_files(tmp_path):
    root = tmp_path / "hermes"
    logs = root / "state" / "nw-agent-dispatch" / "logs"
    for i in range(10):
        _touch(logs / f"lane-{i}.log", OLD - i)
    report = lane_dispatch_logs(_cfg(root, execute=True), root)
    assert report.candidate_files == 0  # all inside newest-25
    assert len(list(logs.iterdir())) == 10


def test_dispatch_logs_execute_archives_then_deletes(tmp_path):
    root = tmp_path / "hermes"
    logs = root / "state" / "nw-agent-dispatch" / "logs"
    for i in range(26):
        _touch(logs / f"lane-{i:02d}.log", OLD - i)
    stale_md = _touch(logs / "lane-final.md", OLD - 40)
    cfg = _cfg(root, keep_min=25, execute=True)
    report = lane_dispatch_logs(cfg, root)
    assert report.acted_files == 2
    assert not stale_md.exists()
    receipts = list(cfg.archive_dir.glob("*.receipt.json"))
    assert receipts
    classes = {json.loads(p.read_text())["class"] for p in receipts}
    assert classes == {CLASS_BULK, CLASS_RECEIPT}  # .log bulk, .md receipt
    # archived content is restorable and root-relative
    tars = list(cfg.archive_dir.glob("*.tar.gz"))
    names = set()
    for t in tars:
        with tarfile.open(t) as tar:
            names.update(tar.getnames())
    assert any(n.endswith("lane-final.md") for n in names)
    assert all(not n.startswith("/") for n in names)


def test_dispatch_logs_skips_symlinks(tmp_path):
    root = tmp_path / "hermes"
    logs = root / "state" / "nw-agent-dispatch" / "logs"
    logs.mkdir(parents=True)
    outside = _touch(tmp_path / "outside.log", OLD)
    for i in range(30):
        _touch(logs / f"pad-{i}.log", FRESH)
    (logs / "escape.log").symlink_to(outside)
    os.utime(logs / "escape.log", (OLD, OLD), follow_symlinks=False)
    report = lane_dispatch_logs(_cfg(root, execute=True), root)
    assert report.candidate_files == 0
    assert outside.exists()


# ---------------------------------------------------------------------------
# lane-workdirs
# ---------------------------------------------------------------------------

def _make_workdir(root, lane_id, mtime):
    wd = root / "state" / "nw-agent-dispatch" / "lane-workdirs" / lane_id
    _touch(wd / "launch-receipt.json", mtime, b'{"lane_id": "%s"}' % lane_id.encode())
    _touch(wd / "scratch.txt", mtime)
    os.utime(wd, (mtime, mtime))
    return wd


def test_lane_workdir_inside_grace_survives(tmp_path):
    root = tmp_path / "hermes"
    wd = _make_workdir(root, "lane-fresh", FRESH)
    report = lane_lane_workdirs(_cfg(root, execute=True), root)
    assert wd.exists()
    assert report.acted_files == 0
    assert any("grace" in n for n in report.notes)


def test_lane_workdir_registry_alive_survives(tmp_path):
    root = tmp_path / "hermes"
    wd = _make_workdir(root, "lane-live", OLD)
    registry = root / "state" / "nw-agent-dispatch" / "agent-work-registry.json"
    registry.write_text(json.dumps(
        {"agents": [{"lane": "lane-live", "alive": True}]}
    ))
    report = lane_lane_workdirs(_cfg(root, execute=True), root)
    assert wd.exists()
    assert any("alive" in n for n in report.notes)


def test_lane_workdir_registry_mentions_without_verdict_survives(tmp_path):
    root = tmp_path / "hermes"
    wd = _make_workdir(root, "lane-ambiguous", OLD)
    registry = root / "state" / "nw-agent-dispatch" / "agent-work-registry.json"
    registry.write_text(json.dumps({"notes": ["lane-ambiguous seen"]}))
    lane_lane_workdirs(_cfg(root, execute=True), root)
    assert wd.exists()  # fail-safe: mentioned but no liveness verdict


def test_lane_workdir_dead_lane_archived_and_removed(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    wd = _make_workdir(root, "lane-dead", OLD)
    registry = root / "state" / "nw-agent-dispatch" / "agent-work-registry.json"
    registry.write_text(json.dumps(
        {"agents": [{"lane": "lane-dead", "alive": False,
                     "root_process_alive": False}]}
    ))
    monkeypatch.setattr(
        "neoengine_local.hermes_home_retention._process_sweep_mentions",
        lambda needles: False,
    )
    cfg = _cfg(root, execute=True)
    report = lane_lane_workdirs(cfg, root)
    assert not wd.exists()
    assert report.acted_files == 1
    archives = list(cfg.archive_dir.glob("lane-workdir-lane-dead-*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0]) as tar:
        names = tar.getnames()
    assert any("launch-receipt.json" in n for n in names)
    assert any("status.txt" in n for n in names)  # git snapshot member
    receipt = json.loads(
        next(cfg.archive_dir.glob("lane-workdir-lane-dead-*.receipt.json"))
        .read_text()
    )
    assert receipt["class"] == CLASS_RECEIPT


def test_lane_workdir_live_process_survives(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    wd = _make_workdir(root, "lane-proc", OLD)
    monkeypatch.setattr(
        "neoengine_local.hermes_home_retention._process_sweep_mentions",
        lambda needles: True,
    )
    lane_lane_workdirs(_cfg(root, execute=True), root)
    assert wd.exists()


# ---------------------------------------------------------------------------
# cron-output
# ---------------------------------------------------------------------------

def test_cron_output_prunes_old_and_rmdirs_empty(tmp_path):
    root = tmp_path / "hermes"
    job = root / "cron" / "output" / "abc123"
    for i in range(30):
        _touch(job / f"2026-05-{i:02d}_00-00-00.md", OLD - i)
    cfg = _cfg(root, keep_min=25, execute=True)
    report = lane_cron_output(cfg, root)
    assert report.acted_files == 5
    assert len(list(job.iterdir())) == 25
    empty_job = root / "cron" / "output" / "empty1"
    stale = _touch(empty_job / "only.md", OLD)
    for i in range(25):
        _touch(empty_job / f"pad-{i}.md", OLD - 100 - i)
    # all 26 old, keep 25 newest => 1 pruned; dir non-empty so stays
    report2 = lane_cron_output(cfg, root)
    assert (root / "cron" / "output" / "empty1").exists()
    assert stale.exists()  # stale is the newest => survives via keep-min


def test_cron_output_dry_run_counts_only(tmp_path):
    root = tmp_path / "hermes"
    job = root / "cron" / "output" / "abc123"
    for i in range(30):
        _touch(job / f"run-{i:02d}.md", OLD - i)
    report = lane_cron_output(_cfg(root, keep_min=25), root)
    assert report.candidate_files == 5
    assert len(list(job.iterdir())) == 30


# ---------------------------------------------------------------------------
# state-db
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL,
    ended_at REAL, end_reason TEXT, parent_session_id TEXT,
    input_tokens INTEGER DEFAULT 0, system_prompt TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL, content TEXT, tool_calls TEXT,
    reasoning_content TEXT, timestamp REAL NOT NULL
);
CREATE VIRTUAL TABLE messages_fts USING fts5(content);
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, COALESCE(new.content, ''));
END;
CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
END;
"""


def _make_db(root):
    root.mkdir(parents=True, exist_ok=True)
    db = root / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    now = time.time()
    old_ts = now - 45 * DAY
    rows = [
        # ended + old => prunable
        ("old-ended", old_ts, old_ts + 60, "done", None, "SECRET-PROMPT"),
        # ended recently => kept
        ("new-ended", now - DAY, now - DAY + 60, "done", None, None),
        # open with recent message => kept
        ("live-open", old_ts, None, None, None, None),
        # open, started long ago, no recent messages => crashed, prunable
        ("stale-open", old_ts, None, None, None, None),
        # child of the pruned parent => parent link nulled
        ("child", now - DAY, None, None, "old-ended", None),
    ]
    conn.executemany(
        "INSERT INTO sessions (id, started_at, ended_at, end_reason,"
        " parent_session_id, system_prompt, source) VALUES (?,?,?,?,?,?,'t')",
        rows,
    )
    msgs = [
        ("old-ended", "old payload", old_ts),
        ("old-ended", "old payload 2", old_ts),
        ("stale-open", "orphaned", old_ts),
        ("live-open", "recent activity", now - 60),
        ("new-ended", "recent", now - DAY),
    ]
    conn.executemany(
        "INSERT INTO messages (session_id, content, timestamp, role)"
        " VALUES (?,?,?,'assistant')",
        msgs,
    )
    conn.commit()
    conn.close()
    return db


def test_state_db_dry_run_reports_without_mutation(tmp_path):
    root = tmp_path / "hermes"
    db = _make_db(root)
    report = lane_state_db(_cfg(root), root)
    assert report.candidate_files > 0
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 5
    conn.close()


def test_state_db_prunes_ended_and_stale_open_sessions(tmp_path):
    root = tmp_path / "hermes"
    db = _make_db(root)
    cfg = _cfg(root, execute=True, db_max_age_days=30.0)
    report = lane_state_db(cfg, root)
    assert not report.errors
    conn = sqlite3.connect(db)
    remaining = {
        r[0] for r in conn.execute("SELECT id FROM sessions").fetchall()
    }
    assert remaining == {"new-ended", "live-open", "child"}
    assert conn.execute(
        "SELECT parent_session_id FROM sessions WHERE id='child'"
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id IN"
        " ('old-ended','stale-open')"
    ).fetchone()[0] == 0
    # FTS shadow rows followed via triggers
    assert conn.execute(
        "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'payload'"
    ).fetchone()[0] == 0
    conn.close()
    # exports: sessions summary (receipt class, no system_prompt) + messages
    summary = next(cfg.archive_dir.glob("*sessions-summary-*.jsonl.gz"))
    lines = [
        json.loads(l)
        for l in gzip.open(summary, "rt").read().splitlines()
    ]
    assert {row["id"] for row in lines} == {"old-ended", "stale-open"}
    assert all("system_prompt" not in row for row in lines)
    summary_receipt = json.loads(
        next(cfg.archive_dir.glob("*sessions-summary-*.receipt.json"))
        .read_text()
    )
    assert summary_receipt["class"] == CLASS_RECEIPT
    messages_export = next(cfg.archive_dir.glob("*messages-*.jsonl.gz"))
    exported = gzip.open(messages_export, "rt").read()
    assert "old payload" in exported and "orphaned" in exported


def test_state_db_missing_tables_is_note_not_error(tmp_path):
    root = tmp_path / "hermes"
    root.mkdir(parents=True)
    conn = sqlite3.connect(root / "state.db")
    conn.execute("CREATE TABLE other (id INTEGER)")
    conn.commit()
    conn.close()
    report = lane_state_db(_cfg(root, execute=True), root)
    assert not report.errors
    assert any("no sessions/messages" in n for n in report.notes)


# ---------------------------------------------------------------------------
# profile caches
# ---------------------------------------------------------------------------

def test_profile_caches_deleted_without_archive(tmp_path):
    root = tmp_path / "hermes"
    stale = _touch(
        root / "profiles" / "p1" / "home" / ".npm" / "_cacache" / "blob", OLD
    )
    fresh = _touch(root / "profiles" / "p1" / "lsp" / "recent.bin", FRESH)
    cfg = _cfg(root, execute=True)
    report = lane_profile_caches(cfg, root)
    assert not stale.exists()
    assert fresh.exists()
    assert report.acted_files == 1
    assert not cfg.archive_dir.exists()  # cache class => no archive


# ---------------------------------------------------------------------------
# archive gc
# ---------------------------------------------------------------------------

def _write_fake_archive(cfg, slug, klass, mtime):
    cfg.archive_dir.mkdir(parents=True, exist_ok=True)
    archive = cfg.archive_dir / f"{slug}.tar.gz"
    archive.write_bytes(b"tar")
    receipt = cfg.archive_dir / f"{slug}.receipt.json"
    receipt.write_text(json.dumps(
        {"class": klass, "archive": archive.name}
    ))
    os.utime(receipt, (mtime, mtime))
    os.utime(archive, (mtime, mtime))
    return archive, receipt


def test_archive_gc_prunes_bulk_keeps_receipts(tmp_path):
    root = tmp_path / "hermes"
    cfg = _cfg(root, execute=True, bulk_archive_max_age_days=90.0)
    ancient = time.time() - 120 * DAY
    bulk_a, bulk_r = _write_fake_archive(cfg, "logs-old", CLASS_BULK, ancient)
    keep_a, keep_r = _write_fake_archive(
        cfg, "receipts-old", CLASS_RECEIPT, ancient
    )
    recent_a, recent_r = _write_fake_archive(
        cfg, "logs-new", CLASS_BULK, time.time() - DAY
    )
    report = lane_archive_gc(cfg)
    assert not bulk_a.exists() and not bulk_r.exists()
    assert keep_a.exists() and keep_r.exists()
    assert recent_a.exists() and recent_r.exists()
    assert report.acted_files == 1


# ---------------------------------------------------------------------------
# kill switches, orchestration, CLI
# ---------------------------------------------------------------------------

def test_global_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME_RETENTION_DISABLED", "1")
    root = tmp_path / "hermes"
    _touch(root / "state" / "nw-agent-dispatch" / "logs" / "a.log", OLD)
    reports = run_retention(_cfg(root, execute=True))
    assert len(reports) == 1 and not reports[0].enabled
    assert (root / "state" / "nw-agent-dispatch" / "logs" / "a.log").exists()


def test_lane_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME_RETENTION_DISPATCH_LOGS_DISABLED", "true")
    root = tmp_path / "hermes"
    report = lane_dispatch_logs(_cfg(root, execute=True), root)
    assert not report.enabled


def test_run_retention_writes_run_receipt_on_execute(tmp_path):
    root = tmp_path / "hermes"
    root.mkdir(parents=True)
    cfg = _cfg(root, execute=True)
    run_retention(cfg)
    runs = list(
        (root / "state" / "hermes-home-retention" / "runs").glob("*.json")
    )
    assert len(runs) == 1
    payload = json.loads(runs[0].read_text())
    assert payload["schema"] == "hermes.home_retention_run.v1"
    assert payload["execute"] is True


def test_check_mode_exit_codes(tmp_path):
    root = tmp_path / "hermes"
    logs = root / "state" / "nw-agent-dispatch" / "logs"
    logs.mkdir(parents=True)
    assert main(["--root", str(root), "--check"]) == 0
    for i in range(60):
        _touch(logs / f"big-{i}.log", OLD - i)
    assert main(
        ["--root", str(root), "--check", "--warn-files", "10"]
    ) == 1
    # check mutates nothing
    assert len(list(logs.iterdir())) == 60


def test_check_and_execute_mutually_exclusive(tmp_path):
    with pytest.raises(SystemExit):
        main(["--root", str(tmp_path), "--check", "--execute"])


def test_missing_root_is_not_failure(tmp_path):
    assert main(["--root", str(tmp_path / "absent"), "--check"]) == 0


def test_profiles_recursion(tmp_path):
    root = tmp_path / "hermes"
    job = root / "profiles" / "p1" / "cron" / "output" / "j1"
    for i in range(30):
        _touch(job / f"run-{i:02d}.md", OLD - i)
    _make_db(root / "profiles" / "p1")
    cfg = _cfg(root, execute=True)
    reports = {r.lane: r for r in run_retention(cfg)}
    assert reports["cron-output[p1]"].acted_files == 5
    assert reports["state-db[p1]"].acted_files > 0
