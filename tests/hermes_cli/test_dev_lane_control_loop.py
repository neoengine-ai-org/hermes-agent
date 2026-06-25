import os
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    kb.init_db()
    return tmp_path


def test_new_session_resumes_from_heartbeat_and_continuity_state(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect() as conn:
        kb.record_lane_heartbeat(
            conn,
            lane_id="dev-a",
            agent_session_id="session-old",
            repo_scope="repo/main",
            state="working",
            claimed_work_item_id="W1",
            evidence_path="receipts/w1.md",
        )
        kb.write_lane_continuity_packet(
            conn,
            lane_id="dev-a",
            packet={
                "current_objective": "fix CI",
                "current_repo_branch_pr": "repo/main#1",
                "files_touched_or_planned": ["a.py"],
                "active_blocker": None,
                "last_verified_command_check": "pytest tests/x.py",
                "next_safe_action": "rerun focused test",
                "explicit_non_claims": ["not merged"],
                "operator_approvals_relied_on": [],
            },
        )
        state = kb.discover_lane_session_state(conn, "dev-a", "session-new")
    assert state["heartbeat"]["claimed_work_item_id"] == "W1"
    assert state["continuity_packet"]["next_safe_action"] == "rerun focused test"
    assert state["ownership_valid"] is False


def test_duplicate_active_claims_are_rejected(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W1", repo_scope="repo", status="open")
        first = kb.claim_lane_work_item(conn, "W1", lane_id="dev-a", claim_owner="s1", ttl_seconds=300, evidence_path="r1.md")
        second = kb.claim_lane_work_item(conn, "W1", lane_id="dev-b", claim_owner="s2", ttl_seconds=300, evidence_path="r2.md")
    assert first is not None and first["claimed"] is True
    assert second["claimed"] is False
    assert second["reason"] == "active_claim_exists"


def test_stale_claims_can_be_recovered_with_evidence(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W1", repo_scope="repo", priority=1, now=900)
        first = kb.pickup_next_lane_work(
            conn,
            lane_id="dev-a",
            agent_session_id="s1",
            authorized_scopes=["repo"],
            ttl_seconds=1,
            evidence_path="old.md",
            now=1000,
        )
        recovered = kb.recover_stale_lane_claims(conn, now=1005, evidence_path="receipts/recovered.md")
        rows = conn.execute("SELECT claim_status, claim_evidence_path FROM lane_claims WHERE work_item_id='W1' ORDER BY id").fetchall()
        item = conn.execute("SELECT status FROM lane_work_items WHERE work_item_id='W1'").fetchone()
        second = kb.pickup_next_lane_work(
            conn,
            lane_id="dev-b",
            agent_session_id="s2",
            authorized_scopes=["repo"],
            ttl_seconds=300,
            evidence_path="second.md",
            now=1006,
        )
    assert first is not None and first["work_item_id"] == "W1"
    assert recovered == ["W1"]
    assert rows[0]["claim_status"] == "expired/recovered"
    assert rows[0]["claim_evidence_path"] == "receipts/recovered.md"
    assert item["status"] == "open"
    assert second is not None and second["work_item_id"] == "W1"
    assert second["claim"]["claimed"] is True


def test_claim_lane_work_item_marks_work_item_claimed_immediately(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W1", repo_scope="repo", priority=1, status="open", now=900)
        result = kb.claim_lane_work_item(
            conn,
            "W1",
            lane_id="dev-a",
            claim_owner="s1",
            ttl_seconds=300,
            evidence_path="claim.md",
            now=1000,
        )
        item = conn.execute("SELECT status, updated_at FROM lane_work_items WHERE work_item_id='W1'").fetchone()
    assert result["claimed"] is True
    assert item["status"] == "claimed"
    assert item["updated_at"] == 1000


def test_direct_claim_requires_existing_pickable_non_governance_held_work(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect() as conn:
        missing = kb.claim_lane_work_item(conn, "missing", lane_id="dev-a", claim_owner="s1", ttl_seconds=300, evidence_path="missing.md")
        kb.upsert_lane_work_item(conn, work_item_id="done", repo_scope="repo", status="completed")
        terminal = kb.claim_lane_work_item(conn, "done", lane_id="dev-a", claim_owner="s1", ttl_seconds=300, evidence_path="done.md")
        kb.upsert_lane_work_item(conn, work_item_id="held", repo_scope="repo", status="open", governance_state="do-not-merge")
        held = kb.claim_lane_work_item(conn, "held", lane_id="dev-a", claim_owner="s1", ttl_seconds=300, evidence_path="held.md")
        active_claims = conn.execute("SELECT COUNT(*) AS n FROM lane_claims WHERE claim_status='active'").fetchone()["n"]
    assert missing["claimed"] is False
    assert missing["reason"] == "work_item_missing"
    assert missing["result"] == "WORK_ITEM_NOT_FOUND"
    assert terminal["claimed"] is False
    assert terminal["reason"] == "work_item_not_claimable"
    assert terminal["result"] == "WORK_ITEM_INELIGIBLE"
    assert held["claimed"] is False
    assert held["reason"] == "governance_held"
    assert held["result"] == "WORK_ITEM_INELIGIBLE"
    assert active_claims == 0


def test_direct_claim_rejects_blocked_work_without_new_event(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="blocked", repo_scope="repo", status="blocked", blocked_event_id=None, now=900)
        result = kb.claim_lane_work_item(conn, "blocked", lane_id="dev-a", claim_owner="s1", ttl_seconds=300, evidence_path="blocked.md", now=1000)
        item = conn.execute("SELECT status FROM lane_work_items WHERE work_item_id='blocked'").fetchone()
        active_claims = conn.execute("SELECT COUNT(*) AS n FROM lane_claims WHERE work_item_id='blocked' AND claim_status='active'").fetchone()["n"]
    assert result["claimed"] is False
    assert result["reason"] == "blocked_without_new_event"
    assert result["result"] == "WORK_ITEM_INELIGIBLE"
    assert item["status"] == "blocked"
    assert active_claims == 0


def test_close_lane_claim_updates_work_item_status(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W1", repo_scope="repo", status="open", now=900)
        claim = kb.claim_lane_work_item(conn, "W1", lane_id="dev-a", claim_owner="s1", ttl_seconds=300, evidence_path="claim.md", now=1000)
        closed = kb.close_lane_claim(conn, claim["claim_id"], status="completed", evidence_path="done.md", now=1100)
        item = conn.execute("SELECT status, updated_at FROM lane_work_items WHERE work_item_id='W1'").fetchone()
        active_claims = conn.execute("SELECT COUNT(*) AS n FROM lane_claims WHERE work_item_id='W1' AND claim_status='active'").fetchone()["n"]
    assert closed is True
    assert active_claims == 0
    assert item["status"] == "completed"
    assert item["updated_at"] == 1100


def test_idle_lanes_back_off_rather_than_spin(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    now = 1000
    with kb.connect() as conn:
        hb = kb.record_lane_heartbeat(conn, lane_id="dev-a", agent_session_id="s1", repo_scope="repo", state="idle-no-work", now=now)
        hb2 = kb.record_lane_heartbeat(conn, lane_id="dev-a", agent_session_id="s1", repo_scope="repo", state="idle-no-work", now=now + 60)
    assert hb["next_eligible_wake_time"] > now
    assert hb2["next_eligible_wake_time"] - (now + 60) >= hb["next_eligible_wake_time"] - now


def test_event_wake_beats_timer_wake(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    now = 1000
    with kb.connect() as conn:
        kb.record_lane_heartbeat(conn, lane_id="dev-a", agent_session_id="s1", repo_scope="repo", state="idle-no-work", now=now)
        kb.upsert_lane_work_item(conn, work_item_id="W2", repo_scope="repo", status="open", now=now)
        kb.record_lane_event(conn, lane_id="dev-a", event_type="new_repair_packet", work_item_id="W2", evidence_path="events/e1.json", now=now + 10)
        wake = kb.next_lane_wake(conn, "dev-a", now=now + 11)
    assert wake["wake_reason"] == "event:new_repair_packet"
    assert wake["eligible_now"] is True


def test_blocked_work_is_not_repicked_without_new_event(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W1", repo_scope="repo", priority=10, status="blocked", blocked_event_id=None)
        assert kb.pickup_next_lane_work(conn, lane_id="dev-a", agent_session_id="s1", authorized_scopes=["repo"]) is None
        kb.record_lane_event(conn, lane_id="dev-a", event_type="governance_unblock", work_item_id="W1", now=2000)
        picked = kb.pickup_next_lane_work(conn, lane_id="dev-a", agent_session_id="s1", authorized_scopes=["repo"])
    assert picked["work_item_id"] == "W1"


def test_lane_status_report_classifies_states(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    now = 2000
    with kb.connect() as conn:
        kb.record_lane_heartbeat(conn, lane_id="working", agent_session_id="s1", repo_scope="repo", state="working", claimed_work_item_id="W1", now=now)
        kb.upsert_lane_work_item(conn, work_item_id="W1", repo_scope="repo", status="open", now=now)
        kb.claim_lane_work_item(conn, "W1", lane_id="working", claim_owner="s1", ttl_seconds=300, evidence_path="w.md", now=now)
        kb.record_lane_heartbeat(conn, lane_id="idle", agent_session_id="s2", repo_scope="repo", state="idle-no-work", now=now)
        kb.record_lane_heartbeat(conn, lane_id="stale", agent_session_id="s3", repo_scope="repo", state="working", now=now - 7200)
        report = kb.lane_status_report(conn, now=now)
    classes = {row["lane_id"]: row["classification"] for row in report["lanes"]}
    assert classes["working"] == "working"
    assert classes["idle"] == "idle-no-work"
    assert classes["stale"] == "stale"


def test_no_governance_bypass_cases_are_preserved(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="D1", repo_scope="repo", priority=99, governance_state="do-not-merge")
        kb.upsert_lane_work_item(conn, work_item_id="H1", repo_scope="repo", priority=98, governance_state="awaiting-human")
        picked = kb.pickup_next_lane_work(conn, lane_id="dev-a", agent_session_id="s1", authorized_scopes=["repo"])
    assert picked is None


def test_claim_race_integrity_error_returns_structured_duplicate(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W-race", repo_scope="repo", status="open", now=900)
        conn.execute(
            """
            CREATE TEMP TRIGGER simulate_lane_claim_race
            BEFORE INSERT ON lane_claims
            WHEN NEW.claim_owner='race-session'
            BEGIN
                INSERT INTO lane_claims(
                    work_item_id, lane_id, claim_owner, claim_started_at,
                    claim_expires_at, claim_status, claim_evidence_path
                ) VALUES (NEW.work_item_id, 'dev-other', 'other-session', NEW.claim_started_at,
                          NEW.claim_expires_at, 'active', 'other.md');
            END
            """
        )
        result = kb.claim_lane_work_item(
            conn,
            "W-race",
            lane_id="dev-a",
            claim_owner="race-session",
            ttl_seconds=300,
            evidence_path="race.md",
            now=1000,
        )
        rows = conn.execute("SELECT lane_id, claim_owner FROM lane_claims WHERE work_item_id='W-race'").fetchall()
    assert result["claimed"] is False
    assert result["result"] == "CLAIM_TRANSACTION_FAILED"
    assert "active_claim" not in result
    assert rows == []


def _claim_same_work_worker(args):
    import os
    from hermes_cli import kanban_db as worker_kb

    home, lane_id = args
    os.environ["HERMES_KANBAN_HOME"] = home
    with worker_kb.connect() as conn:
        return worker_kb.claim_lane_work_item(
            conn,
            "W-concurrent",
            lane_id=lane_id,
            claim_owner=f"{lane_id}-session",
            ttl_seconds=300,
            evidence_path=f"{lane_id}.md",
            now=2000,
        )


def test_claim_results_expose_stable_domain_codes(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect() as conn:
        missing = kb.claim_lane_work_item(conn, "missing", lane_id="dev-a", claim_owner="s1", ttl_seconds=300, evidence_path="missing.md")
        kb.upsert_lane_work_item(conn, work_item_id="held", repo_scope="repo", status="open", governance_state="awaiting-human")
        held = kb.claim_lane_work_item(conn, "held", lane_id="dev-a", claim_owner="s1", ttl_seconds=300, evidence_path="held.md")
        kb.upsert_lane_work_item(conn, work_item_id="W1", repo_scope="repo", status="open")
        first = kb.claim_lane_work_item(conn, "W1", lane_id="dev-a", claim_owner="s1", ttl_seconds=300, evidence_path="one.md")
        duplicate = kb.claim_lane_work_item(conn, "W1", lane_id="dev-b", claim_owner="s2", ttl_seconds=300, evidence_path="two.md")
    assert missing["result"] == "WORK_ITEM_NOT_FOUND"
    assert held["result"] == "WORK_ITEM_INELIGIBLE"
    assert first["result"] == "CLAIMED"
    assert duplicate["result"] == "ACTIVE_CLAIM_EXISTS"


def test_event_is_consumed_after_successful_claim_and_duplicate_wake_is_harmless(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W-event", repo_scope="repo", status="blocked", blocked_event_id=None, now=900)
        event = kb.record_lane_event(conn, lane_id="dev-a", event_type="governance_unblock", work_item_id="W-event", now=1000)
        picked = kb.pickup_next_lane_work(conn, lane_id="dev-a", agent_session_id="s1", authorized_scopes=["repo"], now=1001)
        duplicate = kb.claim_lane_work_item(conn, "W-event", lane_id="dev-b", claim_owner="s2", ttl_seconds=300, evidence_path="dup.md", now=1002)
        ev = conn.execute("SELECT consumed_at FROM lane_events WHERE id=?", (event["event_id"],)).fetchone()
    assert picked is not None and picked["work_item_id"] == "W-event"
    assert ev["consumed_at"] == 1001
    assert duplicate["result"] == "ACTIVE_CLAIM_EXISTS"


def test_multiprocess_same_item_claim_has_exactly_one_active_claim_and_integrity_ok(tmp_path, monkeypatch):
    import multiprocessing as mp

    _home(tmp_path, monkeypatch)
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W-concurrent", repo_scope="repo", status="open", now=1000)
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=4) as pool:
        results = pool.map(_claim_same_work_worker, [(str(tmp_path), f"dev-{idx}") for idx in range(4)])
    with kb.connect() as conn:
        active = conn.execute("SELECT COUNT(*) AS n FROM lane_claims WHERE work_item_id='W-concurrent' AND claim_status='active'").fetchone()["n"]
        status = conn.execute("SELECT status FROM lane_work_items WHERE work_item_id='W-concurrent'").fetchone()["status"]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    assert sum(1 for result in results if result["claimed"] is True) == 1
    assert active == 1
    assert status == "claimed"
    assert integrity == "ok"


def test_consumed_blocked_event_cannot_reclaim_same_blocked_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.record_lane_heartbeat(conn, lane_id="lane-a", agent_session_id="sess-a", repo_scope="repo", state="idle-no-work", now=999)
        kb.upsert_lane_work_item(conn, work_item_id="W-blocked-event", repo_scope="repo", status="blocked", blocked_event_id=None)
        event = kb.record_lane_event(conn, lane_id=None, work_item_id="W-blocked-event", event_type="new_repair_packet", now=1000)
        assert event["event_id"] == 1
        first = kb.pickup_next_lane_work(conn, lane_id="lane-a", agent_session_id="sess-a", authorized_scopes=["repo"], ttl_seconds=300, now=1001)
        assert first is not None and first["claim"]["claimed"] is True
        assert conn.execute("SELECT consumed_at FROM lane_events WHERE id = 1").fetchone()[0] == 1001.0
        assert kb.close_lane_claim(conn, claim_id=first["claim"]["claim_id"], status="blocked", evidence_path="receipts/blocked.md", now=1002) is True
        blocked = conn.execute("SELECT * FROM lane_work_items WHERE work_item_id='W-blocked-event'").fetchone()
        assert blocked["blocked_event_id"] == 1
        second = kb.pickup_next_lane_work(conn, lane_id="lane-a", agent_session_id="sess-b", authorized_scopes=["repo"], ttl_seconds=300, now=1003)
        assert second is None


def test_new_event_during_active_claim_remains_pickable_after_blocked_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.record_lane_heartbeat(conn, lane_id="lane-a", agent_session_id="sess-a", repo_scope="repo", state="idle-no-work", now=999)
        kb.upsert_lane_work_item(conn, work_item_id="W-blocked-race", repo_scope="repo", status="blocked", blocked_event_id=None)
        first_event = kb.record_lane_event(conn, lane_id=None, work_item_id="W-blocked-race", event_type="new_repair_packet", now=1000)
        first = kb.pickup_next_lane_work(conn, lane_id="lane-a", agent_session_id="sess-a", authorized_scopes=["repo"], ttl_seconds=300, now=1001)
        assert first is not None and first["claim"]["claimed"] is True
        assert first_event["event_id"] == 1
        assert conn.execute("SELECT consumed_at FROM lane_events WHERE id = 1").fetchone()[0] == 1001.0

        second_event = kb.record_lane_event(conn, lane_id=None, work_item_id="W-blocked-race", event_type="followup_repair_packet", now=1002)
        assert second_event["event_id"] == 2
        assert kb.close_lane_claim(conn, claim_id=first["claim"]["claim_id"], status="blocked", evidence_path="receipts/blocked.md", now=1003) is True

        blocked = conn.execute("SELECT * FROM lane_work_items WHERE work_item_id='W-blocked-race'").fetchone()
        assert blocked["blocked_event_id"] == 1
        assert blocked["last_event_id"] == 2
        assert conn.execute("SELECT consumed_at FROM lane_events WHERE id = 2").fetchone()[0] is None

        second = kb.pickup_next_lane_work(conn, lane_id="lane-a", agent_session_id="sess-b", authorized_scopes=["repo"], ttl_seconds=300, now=1004)
        assert second is not None and second["work_item_id"] == "W-blocked-race"
        assert conn.execute("SELECT consumed_at FROM lane_events WHERE id = 2").fetchone()[0] == 1004.0


def test_direct_blocked_claim_closeout_baselines_processed_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W-direct-blocked", repo_scope="repo", status="blocked", blocked_event_id=None)
        event = kb.record_lane_event(conn, lane_id=None, work_item_id="W-direct-blocked", event_type="governance_unblock", now=1000)
        first = kb.claim_lane_work_item(conn, "W-direct-blocked", lane_id="lane-a", claim_owner="sess-a", ttl_seconds=300, evidence_path="direct.md", now=1001)
        assert first["claimed"] is True
        assert event["event_id"] == 1
        assert kb.close_lane_claim(conn, claim_id=first["claim_id"], status="blocked", evidence_path="blocked.md", now=1002) is True

        blocked = conn.execute("SELECT * FROM lane_work_items WHERE work_item_id='W-direct-blocked'").fetchone()
        assert blocked["status"] == "blocked"
        assert blocked["blocked_event_id"] == 1
        assert blocked["last_event_id"] == 1
        assert conn.execute("SELECT consumed_at FROM lane_events WHERE id = 1").fetchone()[0] == 1001.0
        assert kb.next_lane_wake(conn, "lane-b", now=1003)["wake_reason"] == "timer"

        second = kb.claim_lane_work_item(conn, "W-direct-blocked", lane_id="lane-b", claim_owner="sess-b", ttl_seconds=300, evidence_path="second.md", now=1003)
        assert second["claimed"] is False
        assert second["result"] == "WORK_ITEM_INELIGIBLE"
        assert second["reason"] == "blocked_without_new_event"


def test_duplicate_active_legacy_claim_index_conflict_is_structured(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-duplicates.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE lane_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            work_item_id TEXT NOT NULL,
            lane_id TEXT NOT NULL,
            claim_owner TEXT NOT NULL,
            claim_started_at INTEGER NOT NULL,
            claim_expires_at INTEGER NOT NULL,
            claim_status TEXT NOT NULL,
            claim_evidence_path TEXT NOT NULL,
            closed_at INTEGER,
            recovery_of_claim_id INTEGER,
            close_message TEXT
        );
        INSERT INTO lane_claims(work_item_id, lane_id, claim_owner, claim_started_at, claim_expires_at, claim_status, claim_evidence_path)
        VALUES ('W-dup', 'lane-a', 'sess-a', 1, 100, 'active', 'a.md'),
               ('W-dup', 'lane-b', 'sess-b', 2, 100, 'active', 'b.md');
        """
    )
    con.close()

    with pytest.raises(kb.LaneControlMigrationConflictError) as excinfo:
        kb.connect(db_path)
    assert not isinstance(excinfo.value, sqlite3.IntegrityError)


def test_blocked_event_baseline_survives_work_item_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W-refresh-blocked", repo_scope="repo", status="blocked", blocked_event_id=None, now=900)
        kb.record_lane_event(conn, lane_id=None, work_item_id="W-refresh-blocked", event_type="governance_unblock", now=1000)
        first = kb.claim_lane_work_item(conn, "W-refresh-blocked", lane_id="lane-a", claim_owner="sess-a", ttl_seconds=300, evidence_path="first.md", now=1001)
        assert first["claimed"] is True
        assert kb.close_lane_claim(conn, claim_id=first["claim_id"], status="blocked", evidence_path="blocked.md", now=1002) is True

        kb.upsert_lane_work_item(conn, work_item_id="W-refresh-blocked", repo_scope="repo", status="blocked", priority=5, now=1003)
        refreshed = conn.execute("SELECT * FROM lane_work_items WHERE work_item_id='W-refresh-blocked'").fetchone()
        assert refreshed["blocked_event_id"] == 1
        assert refreshed["last_event_id"] == 1

        second = kb.pickup_next_lane_work(conn, lane_id="lane-b", agent_session_id="sess-b", authorized_scopes=["repo"], now=1004)
        assert second is None


def test_open_event_direct_claim_blocked_closeout_baselines_processed_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W-open-direct-blocked", repo_scope="repo", status="open", now=900)
        kb.record_lane_event(conn, lane_id=None, work_item_id="W-open-direct-blocked", event_type="new_repair_packet", now=1000)
        first = kb.claim_lane_work_item(conn, "W-open-direct-blocked", lane_id="lane-a", claim_owner="sess-a", ttl_seconds=300, evidence_path="direct.md", now=1001)
        assert first["claimed"] is True
        assert kb.close_lane_claim(conn, claim_id=first["claim_id"], status="blocked", evidence_path="blocked.md", now=1002) is True
        blocked = conn.execute("SELECT * FROM lane_work_items WHERE work_item_id='W-open-direct-blocked'").fetchone()
        assert blocked["status"] == "blocked"
        assert blocked["last_event_id"] == 1
        assert blocked["blocked_event_id"] == 1
        assert conn.execute("SELECT consumed_at FROM lane_events WHERE id = 1").fetchone()[0] == 1001.0
        assert kb.pickup_next_lane_work(conn, lane_id="lane-b", agent_session_id="sess-b", authorized_scopes=["repo"], now=1003) is None


def test_new_event_during_direct_claim_remains_pickable_after_blocked_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W-direct-new-event", repo_scope="repo", status="open", now=900)
        kb.record_lane_event(conn, lane_id=None, work_item_id="W-direct-new-event", event_type="new_repair_packet", now=1000)
        first = kb.claim_lane_work_item(conn, "W-direct-new-event", lane_id="lane-a", claim_owner="sess-a", ttl_seconds=300, evidence_path="direct.md", now=1001)
        assert first["claimed"] is True
        kb.record_lane_event(conn, lane_id=None, work_item_id="W-direct-new-event", event_type="followup_repair_packet", now=1002)
        assert kb.close_lane_claim(conn, claim_id=first["claim_id"], status="blocked", evidence_path="blocked.md", now=1003) is True

        blocked = conn.execute("SELECT * FROM lane_work_items WHERE work_item_id='W-direct-new-event'").fetchone()
        assert blocked["status"] == "blocked"
        assert blocked["blocked_event_id"] == 1
        assert blocked["last_event_id"] == 2
        assert conn.execute("SELECT consumed_at FROM lane_events WHERE id = 1").fetchone()[0] == 1001.0
        assert conn.execute("SELECT consumed_at FROM lane_events WHERE id = 2").fetchone()[0] is None

        second = kb.pickup_next_lane_work(conn, lane_id="lane-b", agent_session_id="sess-b", authorized_scopes=["repo"], ttl_seconds=300, now=1004)
        assert second is not None
        assert second["claim"]["claimed"] is True


def test_legacy_active_claim_without_claimed_event_requires_post_closeout_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W-legacy", repo_scope="repo", status="claimed", now=900)
        conn.execute(
            """
            INSERT INTO lane_claims(
                work_item_id, lane_id, claim_owner, claim_started_at,
                claim_expires_at, claim_status, claim_evidence_path, claimed_event_id
            ) VALUES ('W-legacy', 'lane-a', 'sess-a', 900, 1200, 'active', 'legacy.md', NULL)
            """
        )
        claim_id = conn.execute("SELECT id FROM lane_claims WHERE work_item_id='W-legacy'").fetchone()["id"]
        event = kb.record_lane_event(conn, lane_id=None, work_item_id="W-legacy", event_type="followup_repair_packet", now=1000)

        assert kb.close_lane_claim(conn, claim_id=claim_id, status="blocked", evidence_path="blocked.md", now=1001) is True

        blocked = conn.execute("SELECT * FROM lane_work_items WHERE work_item_id='W-legacy'").fetchone()
        assert blocked["status"] == "blocked"
        assert blocked["blocked_event_id"] is None
        assert conn.execute("SELECT consumed_at FROM lane_events WHERE id=?", (event["event_id"],)).fetchone()[0] is None
        second = kb.pickup_next_lane_work(conn, lane_id="lane-b", agent_session_id="sess-b", authorized_scopes=["repo"], now=1002)
        assert second is None
        stale = kb.record_lane_event(conn, lane_id=None, work_item_id="W-legacy", event_type="review_request", now=1000)
        assert stale["result"] == "STALE_EVENT"
        fresh = kb.record_lane_event(conn, lane_id=None, work_item_id="W-legacy", event_type="review_request", now=1002)
        assert fresh["event_id"]
        third = kb.pickup_next_lane_work(conn, lane_id="lane-b", agent_session_id="sess-b", authorized_scopes=["repo"], now=1003)
        assert third is not None and third["work_item_id"] == "W-legacy"


def test_direct_open_claim_consumes_claimed_event_at_claim_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.record_lane_heartbeat(conn, lane_id="lane-a", agent_session_id="sess-a", repo_scope="repo", state="idle-no-work", now=900)
        kb.upsert_lane_work_item(conn, work_item_id="W-open-event", repo_scope="repo", status="open", now=900)
        event = kb.record_lane_event(conn, lane_id="lane-a", work_item_id="W-open-event", event_type="new_repair_packet", now=1000)
        claim = kb.claim_lane_work_item(conn, "W-open-event", lane_id="lane-a", claim_owner="sess-a", ttl_seconds=300, evidence_path="claim.md", now=1001)
        wake = kb.next_lane_wake(conn, "lane-a", now=1002)
        consumed_at = conn.execute("SELECT consumed_at FROM lane_events WHERE id=?", (event["event_id"],)).fetchone()[0]
    assert claim["claimed"] is True
    assert claim["claimed_event_id"] == event["event_id"]
    assert consumed_at == 1001.0
    assert wake["wake_reason"] == "timer"


def test_pickup_consumes_actual_claimed_event_not_stale_candidate_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W-stale-row", repo_scope="repo", status="open", now=900)
        first_event = kb.record_lane_event(conn, lane_id=None, work_item_id="W-stale-row", event_type="new_repair_packet", now=1000)
        conn.execute(
            """
            CREATE TEMP TRIGGER add_event_during_claim
            BEFORE INSERT ON lane_claims
            WHEN NEW.work_item_id='W-stale-row'
            BEGIN
                INSERT INTO lane_events(lane_id, event_type, work_item_id, evidence_path, created_at)
                VALUES (NULL, 'followup_repair_packet', 'W-stale-row', NULL, 1001);
                UPDATE lane_work_items SET last_event_id = (SELECT max(id) FROM lane_events), updated_at=1001
                 WHERE work_item_id='W-stale-row';
            END
            """
        )
        picked = kb.pickup_next_lane_work(conn, lane_id="lane-a", agent_session_id="sess-a", authorized_scopes=["repo"], now=1002)
        claimed_event_id = picked["claim"]["claimed_event_id"]
        events = conn.execute("SELECT id, consumed_at FROM lane_events ORDER BY id").fetchall()
        hb = conn.execute("SELECT last_event_id FROM lane_heartbeats WHERE lane_id='lane-a'").fetchone()
    assert picked is not None and picked["work_item_id"] == "W-stale-row"
    assert claimed_event_id != first_event["event_id"]
    assert events[0]["consumed_at"] == 1002.0
    assert events[1]["consumed_at"] == 1002.0
    assert hb["last_event_id"] == claimed_event_id


def test_upsert_does_not_overwrite_active_claimed_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W-refresh-active", repo_scope="repo", status="open", now=900)
        claim = kb.claim_lane_work_item(conn, "W-refresh-active", lane_id="lane-a", claim_owner="sess-a", ttl_seconds=300, evidence_path="claim.md", now=1000)
        refreshed = kb.upsert_lane_work_item(conn, work_item_id="W-refresh-active", repo_scope="repo", status="open", priority=10, now=1001)
        assert refreshed["status"] == "claimed"
        assert kb.close_lane_claim(conn, claim_id=claim["claim_id"], status="completed", evidence_path="done.md", now=1002) is True
        item = conn.execute("SELECT status, priority FROM lane_work_items WHERE work_item_id='W-refresh-active'").fetchone()
    assert item["status"] == "completed"
    assert item["priority"] == 10


def test_claim_consumes_all_prior_same_work_item_events_but_preserves_newer_and_other_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.record_lane_heartbeat(conn, lane_id="lane-b", agent_session_id="sess-b", repo_scope="repo", state="idle-no-work", now=900)
        kb.upsert_lane_work_item(conn, work_item_id="W-events", repo_scope="repo", status="open", now=900)
        kb.upsert_lane_work_item(conn, work_item_id="W-other", repo_scope="repo", status="open", now=900)
        e1 = kb.record_lane_event(conn, lane_id=None, work_item_id="W-events", event_type="new_repair_packet", now=1000)
        e_other = kb.record_lane_event(conn, lane_id=None, work_item_id="W-other", event_type="new_repair_packet", now=1001)
        e2 = kb.record_lane_event(conn, lane_id=None, work_item_id="W-events", event_type="followup_repair_packet", now=1002)

        claim = kb.claim_lane_work_item(conn, "W-events", lane_id="lane-a", claim_owner="sess-a", ttl_seconds=300, evidence_path="claim.md", now=1003)
        e3 = kb.record_lane_event(conn, lane_id=None, work_item_id="W-events", event_type="governance_unblock", now=1004)
        events = {row["id"]: row["consumed_at"] for row in conn.execute("SELECT id, consumed_at FROM lane_events").fetchall()}
        wake = kb.next_lane_wake(conn, "lane-b", now=1005)

    assert claim["claimed"] is True
    assert claim["claimed_event_id"] == e2["event_id"]
    assert events[e1["event_id"]] == 1003
    assert events[e2["event_id"]] == 1003
    assert events[e_other["event_id"]] is None
    assert events[e3["event_id"]] is None
    assert wake["event_id"] == e_other["event_id"]


def test_sqlite_invalid_event_type_is_rejected_without_unblocking_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W-invalid-event", repo_scope="repo", status="blocked", blocked_event_id=None, now=900)
        invalid = kb.record_lane_event(conn, lane_id=None, work_item_id="W-invalid-event", event_type="not_authorized", now=1000)
        row = conn.execute("SELECT last_event_id, blocked_event_id FROM lane_work_items WHERE work_item_id='W-invalid-event'").fetchone()
        count = conn.execute("SELECT COUNT(*) FROM lane_events WHERE work_item_id='W-invalid-event'").fetchone()[0]
        denied = kb.claim_lane_work_item(conn, "W-invalid-event", lane_id="lane-a", claim_owner="sess-a", ttl_seconds=300, evidence_path="denied.md", now=1001)
        valid = kb.record_lane_event(conn, lane_id=None, work_item_id="W-invalid-event", event_type="governance_unblock", now=1002)
        allowed = kb.claim_lane_work_item(conn, "W-invalid-event", lane_id="lane-a", claim_owner="sess-a", ttl_seconds=300, evidence_path="allowed.md", now=1003)

    assert invalid["recorded"] is False
    assert invalid["result"] == "INVALID_EVENT_TYPE"
    assert count == 0
    assert row["last_event_id"] is None
    assert row["blocked_event_id"] is None
    assert denied["claimed"] is False
    assert denied["reason"] == "blocked_without_new_event"
    assert valid["event_id"] is not None
    assert allowed["claimed"] is True


def test_sqlite_next_lane_wake_ignores_injected_invalid_event_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.record_lane_heartbeat(conn, lane_id="lane-invalid", agent_session_id="sess", repo_scope="repo", state="idle-no-work", last_event_id=0, now=100)
        conn.execute("INSERT INTO lane_events(lane_id, event_type, work_item_id, evidence_path, created_at) VALUES (NULL, 'not_authorized', NULL, NULL, 101)")
        valid = kb.record_lane_event(conn, lane_id=None, work_item_id=None, event_type="new_repair_packet", now=102)
        wake = kb.next_lane_wake(conn, "lane-invalid", now=103)

    assert wake["eligible_now"] is True
    assert wake["event_id"] == valid["event_id"]
    assert wake["wake_reason"] == "event:new_repair_packet"


def test_sqlite_upsert_refresh_preserves_blocked_status_without_new_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W-blocked-refresh", repo_scope="repo", status="open", now=100)
        event = kb.record_lane_event(conn, lane_id=None, work_item_id="W-blocked-refresh", event_type="failed_ci_transition", now=101)
        picked = kb.pickup_next_lane_work(conn, lane_id="lane-blocked", agent_session_id="session-a", authorized_scopes=["repo"], now=102)
        kb.close_lane_claim(conn, picked["claim"]["claim_id"], status="blocked", evidence_path="receipts/blocked.md", now=103)

        refreshed = kb.upsert_lane_work_item(conn, work_item_id="W-blocked-refresh", repo_scope="repo", now=104)
        denied = kb.pickup_next_lane_work(conn, lane_id="lane-blocked", agent_session_id="session-b", authorized_scopes=["repo"], now=105)
        newer = kb.record_lane_event(conn, lane_id=None, work_item_id="W-blocked-refresh", event_type="governance_unblock", now=106)
        refreshed_after_new = kb.upsert_lane_work_item(conn, work_item_id="W-blocked-refresh", repo_scope="repo", now=107)
        allowed = kb.pickup_next_lane_work(conn, lane_id="lane-blocked", agent_session_id="session-c", authorized_scopes=["repo"], now=108)

    assert picked["claim"]["claimed_event_id"] == event["event_id"]
    assert refreshed["status"] == "blocked"
    assert denied is None
    assert newer["event_id"] != event["event_id"]
    assert refreshed_after_new["status"] == "open"
    assert allowed["work_item_id"] == "W-blocked-refresh"
    assert allowed["claim"]["claimed_event_id"] == newer["event_id"]


def test_sqlite_record_lane_event_rejects_backdated_blocked_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W-backdated", repo_scope="repo", status="open", now=100)
        event = kb.record_lane_event(conn, lane_id=None, work_item_id="W-backdated", event_type="failed_ci_transition", now=100)
        picked = kb.pickup_next_lane_work(conn, lane_id="lane", agent_session_id="session-a", authorized_scopes=["repo"], now=101)
        kb.close_lane_claim(conn, picked["claim"]["claim_id"], status="blocked", evidence_path="receipts/blocked.md", now=102)
        stale = kb.record_lane_event(conn, lane_id=None, work_item_id="W-backdated", event_type="governance_unblock", now=50)
        denied = kb.pickup_next_lane_work(conn, lane_id="lane", agent_session_id="session-b", authorized_scopes=["repo"], now=103)

    assert event["event_id"] is not None
    assert stale["recorded"] is False
    assert stale["result"] == "STALE_EVENT"
    assert denied is None


def test_sqlite_rejects_unknown_work_item_and_backdated_claimed_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.record_lane_heartbeat(conn, lane_id="lane-orphan", agent_session_id="session-orphan", repo_scope="repo", state="idle-no-work", last_event_id=0, now=9)
        # Legacy/injected orphan event rows must not be treated as authoritative wakes.
        conn.execute("INSERT INTO lane_events(lane_id, event_type, work_item_id, evidence_path, created_at) VALUES (NULL, 'new_repair_packet', 'missing-legacy', NULL, 9)")
        missing = kb.record_lane_event(conn, lane_id=None, work_item_id="missing", event_type="new_repair_packet", now=10)
        orphan_wake = kb.next_lane_wake(conn, "lane-orphan", now=11)
        missing_count = conn.execute("SELECT COUNT(*) FROM lane_events WHERE work_item_id='missing'").fetchone()[0]
        kb.upsert_lane_work_item(conn, work_item_id="W-claimed-backdated", repo_scope="repo", status="open", now=100)
        e1 = kb.record_lane_event(conn, lane_id=None, work_item_id="W-claimed-backdated", event_type="new_repair_packet", now=100)
        picked = kb.pickup_next_lane_work(conn, lane_id="lane", agent_session_id="session-a", authorized_scopes=["repo"], now=101)
        stale = kb.record_lane_event(conn, lane_id=None, work_item_id="W-claimed-backdated", event_type="governance_unblock", now=50)
        claim_wake = kb.next_lane_wake(conn, "lane-orphan", now=101)
        newer = kb.record_lane_event(conn, lane_id=None, work_item_id="W-claimed-backdated", event_type="governance_unblock", now=104)
        claim_newer_wake = kb.next_lane_wake(conn, "lane-orphan", now=105)
        kb.close_lane_claim(conn, picked["claim"]["claim_id"], status="blocked", evidence_path="r", now=106)
        allowed = kb.pickup_next_lane_work(conn, lane_id="lane", agent_session_id="session-b", authorized_scopes=["repo"], now=107)
    assert missing["recorded"] is False and missing["result"] == "WORK_ITEM_NOT_FOUND"
    assert missing_count == 0
    assert orphan_wake["wake_reason"] == "timer"
    assert e1["event_id"] is not None
    assert stale["recorded"] is False and stale["result"] == "STALE_EVENT"
    assert claim_wake["wake_reason"] == "timer"
    assert newer["event_id"] is not None
    assert claim_newer_wake["wake_reason"] == "timer"
    assert allowed is not None
    assert allowed["claim"]["claimed_event_id"] == newer["event_id"]


def test_sqlite_next_lane_wake_skips_injected_backdated_blocked_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.record_lane_heartbeat(conn, lane_id="lane-stale", agent_session_id="session", repo_scope="repo", state="idle-no-work", last_event_id=0, now=90)
        kb.upsert_lane_work_item(conn, work_item_id="W-injected-stale", repo_scope="repo", status="open", now=100)
        event = kb.record_lane_event(conn, lane_id=None, work_item_id="W-injected-stale", event_type="failed_ci_transition", now=100)
        picked = kb.pickup_next_lane_work(conn, lane_id="lane-a", agent_session_id="session-a", authorized_scopes=["repo"], now=101)
        kb.close_lane_claim(conn, picked["claim"]["claim_id"], status="blocked", evidence_path="receipts/blocked.md", now=102)
        conn.execute("INSERT INTO lane_events(lane_id, event_type, work_item_id, evidence_path, created_at) VALUES (NULL, 'governance_unblock', 'W-injected-stale', NULL, 50)")
        wake = kb.next_lane_wake(conn, "lane-stale", now=103)
        denied = kb.pickup_next_lane_work(conn, lane_id="lane-b", agent_session_id="session-b", authorized_scopes=["repo"], now=104)
    assert event["event_id"] is not None
    assert wake["wake_reason"] == "timer"
    assert denied is None


def test_sqlite_legacy_claim_null_baseline_uses_closeout_frontier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W-legacy-null", repo_scope="repo", status="open", now=100)
        e1 = kb.record_lane_event(conn, lane_id=None, work_item_id="W-legacy-null", event_type="new_repair_packet", now=100)
        picked = kb.pickup_next_lane_work(conn, lane_id="lane-a", agent_session_id="session-a", authorized_scopes=["repo"], now=101)
        conn.execute("UPDATE lane_claims SET claimed_event_id=NULL WHERE id=?", (picked["claim"]["claim_id"],))
        stale = kb.record_lane_event(conn, lane_id=None, work_item_id="W-legacy-null", event_type="governance_unblock", now=50)
        newer = kb.record_lane_event(conn, lane_id=None, work_item_id="W-legacy-null", event_type="governance_unblock", now=104)
        kb.close_lane_claim(conn, picked["claim"]["claim_id"], status="blocked", evidence_path="receipts/blocked.md", now=105)
        row = conn.execute("SELECT blocked_event_id FROM lane_work_items WHERE work_item_id='W-legacy-null'").fetchone()
        allowed = kb.pickup_next_lane_work(conn, lane_id="lane-b", agent_session_id="session-b", authorized_scopes=["repo"], now=106)
    assert e1["event_id"] is not None
    assert stale["recorded"] is False and stale["result"] == "STALE_EVENT"
    assert newer["event_id"] is not None
    assert row["blocked_event_id"] is None
    assert allowed is None


def test_sqlite_next_lane_wake_does_not_hide_older_other_work_item_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.record_lane_heartbeat(conn, lane_id="lane-global-frontier", agent_session_id="session", repo_scope="repo", state="idle-no-work", last_event_id=0, now=90)
        kb.upsert_lane_work_item(conn, work_item_id="B-older", repo_scope="repo", status="open", priority=1, now=100)
        e_b = kb.record_lane_event(conn, lane_id=None, work_item_id="B-older", event_type="new_repair_packet", now=100)
        kb.upsert_lane_work_item(conn, work_item_id="A-newer", repo_scope="repo", status="open", priority=10, now=101)
        e_a = kb.record_lane_event(conn, lane_id=None, work_item_id="A-newer", event_type="new_repair_packet", now=101)
        first = kb.pickup_next_lane_work(conn, lane_id="lane-global-frontier", agent_session_id="session", authorized_scopes=["repo"], now=102)
        kb.close_lane_claim(conn, first["claim"]["claim_id"], status="completed", evidence_path="done.md", now=103)
        wake = kb.next_lane_wake(conn, "lane-global-frontier", now=104)
    assert first["work_item_id"] == "A-newer"
    assert e_a["event_id"] > e_b["event_id"]
    assert wake["wake_reason"] == "event:new_repair_packet"
    assert wake["event_id"] == e_b["event_id"]


def test_sqlite_blocked_missing_event_baseline_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W-missing-baseline", repo_scope="repo", status="blocked", blocked_event_id=999, now=100)
        event = kb.record_lane_event(conn, lane_id=None, work_item_id="W-missing-baseline", event_type="governance_unblock", now=200)
        claim = kb.claim_lane_work_item(conn, "W-missing-baseline", lane_id="lane", claim_owner="session", ttl_seconds=300, evidence_path="claim.md", now=201)
        wake = kb.next_lane_wake(conn, "lane", now=202)
    assert event["recorded"] is False
    assert event["result"] == "STALE_EVENT"
    assert claim["claimed"] is False
    assert wake["wake_reason"] == "timer"


def test_sqlite_refresh_preserves_blocked_when_event_baseline_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W-refresh-missing-baseline", repo_scope="repo", status="blocked", blocked_event_id=999, now=100)
        conn.execute("INSERT INTO lane_events(event_type, work_item_id, created_at) VALUES ('governance_unblock','W-refresh-missing-baseline',200)")
        conn.execute("UPDATE lane_work_items SET last_event_id=last_insert_rowid() WHERE work_item_id='W-refresh-missing-baseline'")
        refreshed = kb.upsert_lane_work_item(conn, work_item_id="W-refresh-missing-baseline", repo_scope="repo", status="open", now=201)
        claim = kb.claim_lane_work_item(conn, "W-refresh-missing-baseline", lane_id="lane", claim_owner="session", ttl_seconds=300, evidence_path="claim.md", now=202)
    assert refreshed["status"] == "blocked"
    assert claim["claimed"] is False
