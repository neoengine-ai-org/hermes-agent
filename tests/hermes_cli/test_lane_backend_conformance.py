from __future__ import annotations

from pathlib import Path

from hermes_cli import kanban_db as kb
from neoengine_local.dev_lane_heartbeat import DevLaneStore, VALID_WAKE_EVENTS


def test_backend_event_vocabularies_remain_in_lockstep() -> None:
    assert kb.LANE_VALID_WAKE_EVENTS == VALID_WAKE_EVENTS


def test_sqlite_backend_conformance_event_claim_and_blocked_refresh(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "sqlite-home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="C-events", repo_scope="repo", status="open", now=900)
        kb.upsert_lane_work_item(conn, work_item_id="C-other", repo_scope="repo", status="open", now=900)
        e1 = kb.record_lane_event(conn, lane_id=None, work_item_id="C-events", event_type="new_repair_packet", now=1000)
        e_other = kb.record_lane_event(conn, lane_id=None, work_item_id="C-other", event_type="new_repair_packet", now=1001)
        e2 = kb.record_lane_event(conn, lane_id=None, work_item_id="C-events", event_type="followup_repair_packet", now=1002)

        claim = kb.claim_lane_work_item(conn, "C-events", lane_id="lane-a", claim_owner="sess-a", ttl_seconds=300, evidence_path="claim.md", now=1003)
        duplicate = kb.claim_lane_work_item(conn, "C-events", lane_id="lane-b", claim_owner="sess-b", ttl_seconds=300, evidence_path="dup.md", now=1004)
        kb.record_lane_event(conn, lane_id=None, work_item_id="C-events", event_type="governance_unblock", now=1005)
        events = {row["id"]: row["consumed_at"] for row in conn.execute("SELECT id, consumed_at FROM lane_events").fetchall()}
        other_wake = kb.next_lane_wake(conn, "lane-c", now=1006)

        invalid = kb.record_lane_event(conn, lane_id=None, work_item_id="C-events", event_type="invalid", now=1007)
        invalid_count = conn.execute("SELECT COUNT(*) FROM lane_events WHERE event_type='invalid'").fetchone()[0]

        assert kb.close_lane_claim(conn, claim_id=claim["claim_id"], status="blocked", evidence_path="blocked.md", now=1008)
        refreshed = kb.upsert_lane_work_item(conn, work_item_id="C-events", repo_scope="repo", status="blocked", now=1009)
        allowed = kb.claim_lane_work_item(conn, "C-events", lane_id="lane-c", claim_owner="sess-c", ttl_seconds=300, evidence_path="allowed.md", now=1010)

    assert claim["claimed"] is True
    assert duplicate["result"] == "ACTIVE_CLAIM_EXISTS"
    assert events[e1["event_id"]] == 1003
    assert events[e2["event_id"]] == 1003
    assert events[e_other["event_id"]] is None
    assert other_wake["event_id"] == e_other["event_id"]
    assert invalid["result"] == "INVALID_EVENT_TYPE"
    assert invalid_count == 0
    assert refreshed["blocked_event_id"] == claim["claimed_event_id"]
    assert allowed["claimed"] is True
    assert allowed["claimed_event_id"] > claim["claimed_event_id"]


def test_file_backend_conformance_event_claim_and_blocked_refresh(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path / "file-store")
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.register_lane("lane-b", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item({"work_item_id": "C-events", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.add_work_item({"work_item_id": "C-other", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.record_event("C-events", "new_repair_packet", "2099-06-24T00:00:00Z", event_id="E1")
    store.record_event("C-other", "new_repair_packet", "2099-06-24T00:01:00Z", event_id="O1")
    store.record_event("C-events", "followup_repair_packet", "2099-06-24T00:02:00Z", event_id="E2")

    claim = store.claim_work("C-events", "lane-a", "sess-a", "2099-06-24T00:03:00Z", 3600, "claims/a.json")
    try:
        store.claim_work("C-events", "lane-b", "sess-b", "2099-06-24T00:04:00Z", 3600, "claims/b.json")
    except ValueError as exc:
        duplicate = str(exc)
    store.record_event("C-events", "governance_unblock", "2099-06-24T00:05:00Z", event_id="E3")
    events = {event["event_id"]: event for event in store.read_json("work/items.json")["C-events"]["events"]}
    other_claim = store.pick_next_work("lane-b", "sess-other", now="2099-06-24T00:06:00Z")

    try:
        store.record_event("C-events", "invalid", "2099-06-24T00:07:00Z", event_id="bad")
    except ValueError as exc:
        invalid = str(exc)

    store.close_claim("C-events", "blocked", "receipts/blocked.md", "2099-06-24T00:08:00Z")
    refreshed = store.add_work_item({"work_item_id": "C-events", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "blocked"})
    allowed = store.pick_next_work("lane-a", "sess-allowed", now="2099-06-24T00:09:00Z")

    assert claim["claimed_event_id"] == "E2"
    assert "active claim already exists" in duplicate
    assert events["E1"]["consumed_at"] == "2099-06-24T00:03:00Z"
    assert events["E2"]["consumed_at"] == "2099-06-24T00:03:00Z"
    assert "consumed_at" not in events["E3"]
    assert other_claim["work_item_id"] == "C-other"
    assert "invalid wake event" in invalid
    assert refreshed["blocked_at_event_id"] == "E2"
    assert allowed["action"] == "claimed"
    assert allowed["claim"]["claimed_event_id"] == "E3"
