from __future__ import annotations

from pathlib import Path

import pytest

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



def test_sqlite_null_baseline_blocked_authority_is_closeout_time(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "sqlite-null-home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="NB", repo_scope="repo", status="open", now=100)
        claim = kb.claim_lane_work_item(
            conn,
            "NB",
            lane_id="lane-a",
            claim_owner="sess-a",
            ttl_seconds=300,
            evidence_path="claim.md",
            now=110,
        )
        assert claim["claimed"] is True
        assert claim["claimed_event_id"] is None
        assert kb.close_lane_claim(conn, claim_id=claim["claim_id"], status="blocked", evidence_path="blocked.md", now=120)

        stale = kb.record_lane_event(conn, lane_id=None, work_item_id="NB", event_type="new_repair_packet", now=119)
        same_ts = kb.record_lane_event(conn, lane_id=None, work_item_id="NB", event_type="review_request", now=120)
        assert stale["result"] == "STALE_EVENT"
        assert same_ts["result"] == "STALE_EVENT"
        assert conn.execute("SELECT COUNT(*) FROM lane_events WHERE work_item_id='NB'").fetchone()[0] == 0

        refreshed = kb.upsert_lane_work_item(conn, work_item_id="NB", repo_scope="repo", status="open", now=130)
        assert refreshed["status"] == "blocked"
        assert kb.next_lane_wake(conn, "lane-a", now=131)["wake_reason"] == "timer"
        assert kb.claim_lane_work_item(
            conn,
            "NB",
            lane_id="lane-b",
            claim_owner="sess-b",
            ttl_seconds=300,
            evidence_path="blocked-too-soon.md",
            now=132,
        )["result"] == "WORK_ITEM_INELIGIBLE"

        newer = kb.record_lane_event(conn, lane_id=None, work_item_id="NB", event_type="queue_priority_change", now=121)
        assert newer["event_id"]
        assert kb.next_lane_wake(conn, "lane-a", now=133)["event_id"] == newer["event_id"]
        claimed = kb.claim_lane_work_item(
            conn,
            "NB",
            lane_id="lane-b",
            claim_owner="sess-b",
            ttl_seconds=300,
            evidence_path="allowed.md",
            now=134,
        )
        assert claimed["claimed"] is True
        assert claimed["claimed_event_id"] == newer["event_id"]


def test_file_null_baseline_blocked_authority_is_closeout_time(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path / "file-null-store")
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.register_lane("lane-b", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item({"work_item_id": "NB", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    claim = store.claim_work("NB", "lane-a", "sess-a", "2099-01-01T00:01:00Z", 3600, "claims/a.json")
    assert claim["claimed_event_id"] is None
    store.close_claim("NB", "blocked", "receipts/blocked.md", "2099-01-01T00:02:00Z")

    with pytest.raises(ValueError, match="older than blocked baseline"):
        store.record_event("NB", "new_repair_packet", "2099-01-01T00:01:59Z", event_id="stale")
    with pytest.raises(ValueError, match="older than blocked baseline"):
        store.record_event("NB", "review_request", "2099-01-01T00:02:00Z", event_id="same-ts")

    refreshed = store.add_work_item({
        "work_item_id": "NB",
        "repo_scope": "repo",
        "authorized_scopes": ["**"],
        "status": "open",
        "events": [{"event_id": "scanner-stale", "event_type": "governance_unblock", "created_at": "2099-01-01T00:01:58Z"}],
    })
    assert refreshed["status"] == "blocked"
    idle = store.pick_next_work("lane-b", "sess-idle", now="2099-01-01T00:02:30Z")
    assert idle["action"] == "idle-no-work"

    newer = store.record_event("NB", "queue_priority_change", "2099-01-01T00:02:01Z", event_id="newer")
    assert newer["event_id"] == "newer"
    claimed = store.pick_next_work("lane-b", "sess-b", now="2099-01-01T00:03:00Z")
    assert claimed["action"] == "claimed"
    assert claimed["work_item_id"] == "NB"
    assert claimed["claim"]["claimed_event_id"] == "newer"


@pytest.mark.parametrize("event_type", sorted(VALID_WAKE_EVENTS))
def test_null_baseline_all_wake_event_types_reopen_only_when_newer(tmp_path: Path, monkeypatch, event_type: str) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / f"sqlite-{event_type}"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="SQL-" + event_type, repo_scope="repo", status="open", now=10)
        claim = kb.claim_lane_work_item(conn, "SQL-" + event_type, lane_id="lane", claim_owner="sess", ttl_seconds=300, evidence_path="c.md", now=11)
        assert kb.close_lane_claim(conn, claim_id=claim["claim_id"], status="blocked", evidence_path="b.md", now=20)
        assert kb.record_lane_event(conn, lane_id=None, work_item_id="SQL-" + event_type, event_type=event_type, now=20)["result"] == "STALE_EVENT"
        assert kb.record_lane_event(conn, lane_id=None, work_item_id="SQL-" + event_type, event_type=event_type, now=21)["event_id"]
        assert kb.claim_lane_work_item(conn, "SQL-" + event_type, lane_id="lane2", claim_owner="sess2", ttl_seconds=300, evidence_path="ok.md", now=22)["claimed"] is True

    store = DevLaneStore(tmp_path / f"file-{event_type}")
    store.register_lane("lane", repo_scope="repo", authorized_scopes=["**"])
    store.register_lane("lane2", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item({"work_item_id": "FILE-" + event_type, "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.claim_work("FILE-" + event_type, "lane", "sess", "2099-01-01T00:00:11Z", 3600, "c.json")
    store.close_claim("FILE-" + event_type, "blocked", "b.md", "2099-01-01T00:00:20Z")
    with pytest.raises(ValueError):
        store.record_event("FILE-" + event_type, event_type, "2099-01-01T00:00:20Z", event_id="same")
    store.record_event("FILE-" + event_type, event_type, "2099-01-01T00:00:21Z", event_id="new")
    assert store.pick_next_work("lane2", "sess2", now="2099-01-01T00:00:22Z")["action"] == "claimed"



def test_file_null_baseline_consumed_newer_event_does_not_reopen(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path / "file-consumed-store")
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.register_lane("lane-b", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item({"work_item_id": "NB-consumed", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.claim_work("NB-consumed", "lane-a", "sess-a", "2099-01-01T00:01:00Z", 3600, "claims/a.json")
    store.close_claim("NB-consumed", "blocked", "receipts/blocked.md", "2099-01-01T00:02:00Z")
    refreshed = store.add_work_item({
        "work_item_id": "NB-consumed",
        "repo_scope": "repo",
        "authorized_scopes": ["**"],
        "status": "open",
        "events": [{
            "event_id": "consumed-newer",
            "event_type": "queue_priority_change",
            "created_at": "2099-01-01T00:02:01Z",
            "consumed_at": "2099-01-01T00:02:02Z",
        }],
    })
    assert refreshed["status"] == "blocked"
    assert store.pick_next_work("lane-b", "sess-b", now="2099-01-01T00:03:00Z")["action"] == "idle-no-work"



def test_sqlite_null_baseline_future_event_waits_until_event_time(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "sqlite-future-home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="SQL-future", repo_scope="repo", status="open", now=100)
        assert kb.claim_lane_work_item(conn, "SQL-future", lane_id="lane-a", claim_owner="sess-a", ttl_seconds=300, evidence_path="claim.md", now=110)["claimed"] is True
        claim_id = conn.execute("SELECT id FROM lane_claims WHERE work_item_id='SQL-future'").fetchone()["id"]
        assert kb.close_lane_claim(conn, claim_id=claim_id, status="blocked", evidence_path="blocked.md", now=120) is True
        kb.record_lane_event(conn, lane_id=None, work_item_id="SQL-future", event_type="queue_priority_change", now=180)
        early = kb.claim_lane_work_item(conn, "SQL-future", lane_id="lane-b", claim_owner="sess-b", ttl_seconds=300, evidence_path="early.md", now=150)
        wake_early = kb.next_lane_wake(conn, "lane-b", now=150)
        late = kb.claim_lane_work_item(conn, "SQL-future", lane_id="lane-b", claim_owner="sess-b", ttl_seconds=300, evidence_path="late.md", now=180)
    assert early["claimed"] is False
    assert early["result"] == "WORK_ITEM_INELIGIBLE"
    assert wake_early["wake_reason"] == "timer"
    assert late["claimed"] is True
    assert late["claimed_event_id"] is not None


def test_file_null_baseline_future_event_waits_until_event_time(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path / "file-future-store")
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.register_lane("lane-b", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item({"work_item_id": "FILE-future", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.claim_work("FILE-future", "lane-a", "sess-a", "2099-01-01T00:01:00Z", 3600, "claims/a.json")
    store.close_claim("FILE-future", "blocked", "receipts/blocked.md", "2099-01-01T00:02:00Z")
    store.record_event(work_item_id="FILE-future", event_type="queue_priority_change", created_at="2099-01-01T00:03:00Z")
    early = store.pick_next_work("lane-b", "sess-b", now="2099-01-01T00:02:30Z")
    late = store.pick_next_work("lane-b", "sess-b", now="2099-01-01T00:03:00Z")
    assert early["action"] == "idle-no-work"
    assert late["action"] == "claimed"
    assert late["claim"]["claimed_event_id"]


def test_sqlite_claim_baseline_ignores_preconsumed_direct_rows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "sqlite-consumed-baseline-home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="SQL-consumed", repo_scope="repo", status="blocked", now=100)
        base = kb.record_lane_event(conn, lane_id=None, work_item_id="SQL-consumed", event_type="queue_priority_change", now=105)
        first = kb.claim_lane_work_item(conn, "SQL-consumed", lane_id="lane-a", claim_owner="sess-a", ttl_seconds=300, evidence_path="first.md", now=106)
        assert first["claimed"] is True
        claim_id = conn.execute("SELECT id FROM lane_claims WHERE work_item_id='SQL-consumed' AND claim_status='active'").fetchone()["id"]
        assert kb.close_lane_claim(conn, claim_id=claim_id, status="blocked", evidence_path="blocked.md", now=107) is True
        unconsumed = kb.record_lane_event(conn, lane_id=None, work_item_id="SQL-consumed", event_type="queue_priority_change", now=110)
        conn.execute(
            "INSERT INTO lane_events(lane_id, event_type, work_item_id, evidence_path, created_at, consumed_at) VALUES(NULL, 'queue_priority_change', 'SQL-consumed', NULL, 120, 121)"
        )
        second = kb.claim_lane_work_item(conn, "SQL-consumed", lane_id="lane-b", claim_owner="sess-b", ttl_seconds=300, evidence_path="second.md", now=130)
    assert base["event_id"]
    assert second["claimed"] is True
    assert second["claimed_event_id"] == unconsumed["event_id"]



def test_sqlite_future_event_refresh_does_not_reopen_before_event_time(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "sqlite-future-refresh-home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="SQL-future-refresh", repo_scope="repo", status="open", now=100)
        claim = kb.claim_lane_work_item(conn, "SQL-future-refresh", lane_id="lane-a", claim_owner="sess-a", ttl_seconds=300, evidence_path="claim.md", now=110)
        assert kb.close_lane_claim(conn, claim_id=claim["claim_id"], status="blocked", evidence_path="blocked.md", now=120) is True
        kb.record_lane_event(conn, lane_id=None, work_item_id="SQL-future-refresh", event_type="queue_priority_change", now=180)
        refreshed = kb.upsert_lane_work_item(conn, work_item_id="SQL-future-refresh", repo_scope="repo", status="open", now=150)
        early = kb.claim_lane_work_item(conn, "SQL-future-refresh", lane_id="lane-b", claim_owner="sess-b", ttl_seconds=300, evidence_path="early.md", now=150)
        refreshed_late = kb.upsert_lane_work_item(conn, work_item_id="SQL-future-refresh", repo_scope="repo", status="open", now=180)
    assert refreshed["status"] == "blocked"
    assert early["claimed"] is False
    assert refreshed_late["status"] == "open"


def test_file_future_event_refresh_does_not_reopen_without_refresh_frontier(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path / "file-future-refresh-store")
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.register_lane("lane-b", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item({"work_item_id": "FILE-future-refresh", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.claim_work("FILE-future-refresh", "lane-a", "sess-a", "2099-01-01T00:01:00Z", 3600, "claims/a.json")
    store.close_claim("FILE-future-refresh", "blocked", "receipts/blocked.md", "2099-01-01T00:02:00Z")
    refreshed = store.add_work_item({
        "work_item_id": "FILE-future-refresh",
        "repo_scope": "repo",
        "authorized_scopes": ["**"],
        "status": "open",
        "events": [{"event_id": "future", "event_type": "queue_priority_change", "created_at": "2099-01-01T00:03:00Z"}],
    })
    early = store.pick_next_work("lane-b", "sess-b", now="2099-01-01T00:02:30Z")
    late_refresh = store.add_work_item({
        "work_item_id": "FILE-future-refresh",
        "repo_scope": "repo",
        "authorized_scopes": ["**"],
        "status": "open",
        "updated_at": "2099-01-01T00:03:00Z",
        "events": [{"event_id": "future", "event_type": "queue_priority_change", "created_at": "2099-01-01T00:03:00Z"}],
    })
    assert refreshed["status"] == "blocked"
    assert early["action"] == "idle-no-work"
    assert late_refresh["status"] == "open"



def test_sqlite_null_baseline_closeout_does_not_reconstruct_from_consumed_rows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "sqlite-consumed-closeout-home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="SQL-null-reconstruct", repo_scope="repo", status="open", now=90)
        conn.execute(
            "INSERT INTO lane_events(lane_id, event_type, work_item_id, evidence_path, created_at, consumed_at) VALUES(NULL, 'queue_priority_change', 'SQL-null-reconstruct', NULL, 100, 101)"
        )
        claim = kb.claim_lane_work_item(conn, "SQL-null-reconstruct", lane_id="lane-a", claim_owner="sess-a", ttl_seconds=300, evidence_path="claim.md", now=110)
        assert claim["claimed"] is True
        assert claim["claimed_event_id"] is None
        assert kb.close_lane_claim(conn, claim_id=claim["claim_id"], status="blocked", evidence_path="blocked.md", now=120) is True
        blocked = conn.execute("SELECT status, blocked_event_id, blocked_at FROM lane_work_items WHERE work_item_id='SQL-null-reconstruct'").fetchone()
        stale = kb.record_lane_event(conn, lane_id=None, work_item_id="SQL-null-reconstruct", event_type="review_request", now=116)
        second = kb.claim_lane_work_item(conn, "SQL-null-reconstruct", lane_id="lane-b", claim_owner="sess-b", ttl_seconds=300, evidence_path="second.md", now=117)
    assert blocked["status"] == "blocked"
    assert blocked["blocked_event_id"] is None
    assert blocked["blocked_at"] == 120
    assert stale["result"] == "STALE_EVENT"
    assert second["claimed"] is False



def test_file_null_baseline_closeout_does_not_reconstruct_from_consumed_rows(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path / "file-consumed-closeout-store")
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.register_lane("lane-b", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item({
        "work_item_id": "FILE-null-reconstruct",
        "repo_scope": "repo",
        "authorized_scopes": ["**"],
        "status": "open",
        "events": [{"event_id": "old", "event_type": "queue_priority_change", "created_at": "2099-01-01T00:00:00Z", "consumed_at": "2099-01-01T00:00:01Z"}],
    })
    claim = store.claim_work("FILE-null-reconstruct", "lane-a", "sess-a", "2099-01-01T00:01:00Z", 3600, "claims/a.json")
    assert claim["claimed_event_id"] is None
    store.close_claim("FILE-null-reconstruct", "blocked", "receipts/blocked.md", "2099-01-01T00:02:00Z")
    item = store.read_json("work/items.json", {})["FILE-null-reconstruct"]
    assert item.get("blocked_at_event_id") is None
    assert item.get("blocked_at") == "2099-01-01T00:02:00Z"
    with pytest.raises(ValueError):
        store.record_event("FILE-null-reconstruct", "governance_unblock", "2099-01-01T00:01:30Z")
    assert store.pick_next_work("lane-b", "sess-b", now="2099-01-01T00:01:31Z")["action"] == "idle-no-work"


def test_sqlite_event_baseline_same_timestamp_is_stale(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "sqlite-same-ts-baseline-home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="SQL-same-ts", repo_scope="repo", status="open", now=90)
        base = kb.record_lane_event(conn, lane_id=None, work_item_id="SQL-same-ts", event_type="queue_priority_change", now=100)
        claim = kb.claim_lane_work_item(conn, "SQL-same-ts", lane_id="lane-a", claim_owner="sess-a", ttl_seconds=300, evidence_path="claim.md", now=101)
        assert kb.close_lane_claim(conn, claim_id=claim["claim_id"], status="blocked", evidence_path="blocked.md", now=102) is True
        same = kb.record_lane_event(conn, lane_id=None, work_item_id="SQL-same-ts", event_type="review_request", now=100)
        second = kb.claim_lane_work_item(conn, "SQL-same-ts", lane_id="lane-b", claim_owner="sess-b", ttl_seconds=300, evidence_path="second.md", now=103)
    assert base["event_id"]
    assert same["result"] == "STALE_EVENT"
    assert second["claimed"] is False



def test_sqlite_null_baseline_closeout_clears_stale_blocked_event_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "sqlite-clear-stale-baseline-home"))
    with kb.connect() as conn:
        kb.upsert_lane_work_item(conn, work_item_id="SQL-clear", repo_scope="repo", status="blocked", blocked_event_id=None, now=10)
        event = kb.record_lane_event(conn, lane_id=None, work_item_id="SQL-clear", event_type="queue_priority_change", now=11)
        first = kb.claim_lane_work_item(conn, "SQL-clear", lane_id="lane-a", claim_owner="sess-a", ttl_seconds=300, evidence_path="first.md", now=12)
        assert kb.close_lane_claim(conn, claim_id=first["claim_id"], status="blocked", evidence_path="blocked1.md", now=13) is True
        kb.record_lane_event(conn, lane_id=None, work_item_id="SQL-clear", event_type="queue_priority_change", now=14)
        second = kb.claim_lane_work_item(conn, "SQL-clear", lane_id="lane-b", claim_owner="sess-b", ttl_seconds=300, evidence_path="second.md", now=15)
        conn.execute("UPDATE lane_claims SET claimed_event_id=NULL WHERE id=?", (second["claim_id"],))
        assert kb.close_lane_claim(conn, claim_id=second["claim_id"], status="blocked", evidence_path="blocked2.md", now=17) is True
        row = conn.execute("SELECT blocked_event_id, blocked_at FROM lane_work_items WHERE work_item_id='SQL-clear'").fetchone()
        stale = kb.record_lane_event(conn, lane_id=None, work_item_id="SQL-clear", event_type="review_request", now=16)
    assert event["event_id"]
    assert row["blocked_event_id"] is None
    assert row["blocked_at"] == 17
    assert stale["result"] == "STALE_EVENT"


def test_file_null_baseline_closeout_clears_stale_blocked_event_id(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path / "file-clear-stale-baseline")
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.register_lane("lane-b", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item({"work_item_id": "FILE-clear", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.record_event("FILE-clear", "queue_priority_change", "2099-01-01T00:01:00Z", event_id="E1")
    first = store.pick_next_work("lane-a", "sess-a", now="2099-01-01T00:01:10Z")
    store.close_claim("FILE-clear", "blocked", "blocked1.md", "2099-01-01T00:02:00Z")
    assert first["claim"]["claimed_event_id"] == "E1"
    store.record_event("FILE-clear", "queue_priority_change", "2099-01-01T00:03:00Z", event_id="E2")
    second = store.pick_next_work("lane-b", "sess-b", now="2099-01-01T00:03:10Z")
    claim = store.claim_for_work("FILE-clear")
    claim.pop("claimed_event_id", None)
    store.write_json(str(store._claim_path("FILE-clear")), claim)
    store.close_claim("FILE-clear", "blocked", "blocked2.md", "2099-01-01T00:04:00Z")
    item = store.read_json("work/items.json", {})["FILE-clear"]
    assert second["claim"]["claimed_event_id"] == "E2"
    assert item.get("blocked_at_event_id") is None
    assert item["blocked_at"] == "2099-01-01T00:04:00Z"
    with pytest.raises(ValueError):
        store.record_event("FILE-clear", "review_request", "2099-01-01T00:03:30Z")


def test_file_event_baseline_same_timestamp_is_stale(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path / "file-same-ts-baseline")
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.register_lane("lane-b", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item({"work_item_id": "FILE-same-ts", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.record_event("FILE-same-ts", "queue_priority_change", "2099-01-01T00:01:00Z", event_id="E1")
    store.pick_next_work("lane-a", "sess-a", now="2099-01-01T00:01:10Z")
    store.close_claim("FILE-same-ts", "blocked", "blocked.md", "2099-01-01T00:02:00Z")
    with pytest.raises(ValueError):
        store.record_event("FILE-same-ts", "review_request", "2099-01-01T00:01:00Z", event_id="E2")
    refreshed = store.add_work_item({
        "work_item_id": "FILE-same-ts",
        "repo_scope": "repo",
        "authorized_scopes": ["**"],
        "status": "open",
        "updated_at": "2099-01-01T00:03:00Z",
        "events": [{"event_id": "E2", "event_type": "review_request", "created_at": "2099-01-01T00:01:00Z"}],
    })
    assert refreshed["status"] == "blocked"
    assert store.pick_next_work("lane-b", "sess-b", now="2099-01-01T00:03:00Z")["action"] == "idle-no-work"
