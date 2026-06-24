import os
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
    assert first["claimed"] is True
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
    assert missing == {"claimed": False, "reason": "work_item_missing"}
    assert terminal == {"claimed": False, "reason": "work_item_not_claimable"}
    assert held == {"claimed": False, "reason": "governance_held"}
    assert active_claims == 0


def test_direct_claim_rejects_blocked_work_without_new_event(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="blocked", repo_scope="repo", status="blocked", blocked_event_id=5, now=900)
        result = kb.claim_lane_work_item(conn, "blocked", lane_id="dev-a", claim_owner="s1", ttl_seconds=300, evidence_path="blocked.md", now=1000)
        item = conn.execute("SELECT status FROM lane_work_items WHERE work_item_id='blocked'").fetchone()
        active_claims = conn.execute("SELECT COUNT(*) AS n FROM lane_claims WHERE work_item_id='blocked' AND claim_status='active'").fetchone()["n"]
    assert result == {"claimed": False, "reason": "blocked_without_new_event"}
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
        kb.record_lane_event(conn, lane_id="dev-a", event_type="new_repair_packet", work_item_id="W2", evidence_path="events/e1.json", now=now + 10)
        wake = kb.next_lane_wake(conn, "dev-a", now=now + 11)
    assert wake["wake_reason"] == "event:new_repair_packet"
    assert wake["eligible_now"] is True


def test_blocked_work_is_not_repicked_without_new_event(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="W1", repo_scope="repo", priority=10, status="blocked", blocked_event_id=5)
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
    assert result["reason"] == "active_claim_exists"
    assert result["active_claim"] is None
    assert rows == []
