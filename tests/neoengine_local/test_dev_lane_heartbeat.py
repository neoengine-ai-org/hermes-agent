from __future__ import annotations

import json
from pathlib import Path

import pytest

from neoengine_local.dev_lane_heartbeat import DevLaneStore, utc_parse


def test_new_session_resumes_from_heartbeat_and_continuity_state(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.emit_heartbeat(
        lane_id="lane-a",
        agent_session_id="session-old",
        repo_scope="neoengine-ai-org/neowealth",
        current_state="working",
        claimed_work_item_id="work-1",
        last_successful_activity_at="2026-06-24T00:00:00Z",
        next_eligible_wake_at="2026-06-24T00:10:00Z",
        evidence_pointer="receipts/work-1.md",
    )
    store.write_continuity_packet(
        lane_id="lane-a",
        packet={
            "current_objective": "repair PR CI",
            "current_repo_branch_pr": "neoengine-ai-org/neowealth branch x PR #1",
            "files_touched_or_planned": ["repo/app/page.tsx"],
            "active_blocker": None,
            "last_verified_command_check": "npm test -- pass",
            "next_safe_action": "inspect latest check run",
            "explicit_non_claims": ["not merge-ready"],
            "operator_approvals_relied_on": [],
        },
    )
    store.add_work_item({"work_item_id": "work-1", "repo_scope": "neoengine-ai-org/neowealth", "authorized_scopes": ["repo/app/page.tsx"], "status": "open"})
    store.claim_work(
        work_item_id="work-1",
        lane_id="lane-a",
        owner_session_id="session-old",
        now="2026-06-24T00:00:00Z",
        ttl_seconds=3600,
        evidence_path="claims/work-1.json",
    )

    pickup = store.start_session(lane_id="lane-a", session_id="session-new", now="2026-06-24T00:05:00Z")

    assert pickup["action"] == "resume"
    assert pickup["heartbeat"]["claimed_work_item_id"] == "work-1"
    assert pickup["continuity_packet"]["next_safe_action"] == "inspect latest check run"
    assert pickup["claim"]["claim_owner_session_id"] == "session-old"


def test_duplicate_active_claims_are_rejected(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.add_work_item({"work_item_id": "work-1", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.claim_work("work-1", "lane-a", "session-a", "2026-06-24T00:00:00Z", 3600, "claims/a.json")

    with pytest.raises(ValueError, match="active claim already exists"):
        store.claim_work("work-1", "lane-b", "session-b", "2026-06-24T00:01:00Z", 3600, "claims/b.json")


def test_reserved_history_work_item_id_does_not_overwrite_claim_history(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    original_history = [{"work_item_id": "prior", "claim_status": "completed"}]
    store.write_json("claims/history.json", original_history)

    with pytest.raises(ValueError, match="reserved or unsafe"):
        store.claim_work("history", "lane-a", "session-a", "2026-06-24T00:00:00Z", 3600, "claims/history.json")

    assert store.read_json("claims/history.json") == original_history


@pytest.mark.parametrize("lane_id", ["../lane-a", "lane/a", ".hidden"])
def test_unsafe_lane_ids_are_rejected_before_writing_lane_files(tmp_path: Path, lane_id: str) -> None:
    store = DevLaneStore(tmp_path)

    with pytest.raises(ValueError, match="lane_id must be a safe slug"):
        store.emit_heartbeat(
            lane_id=lane_id,
            agent_session_id="session-a",
            repo_scope="repo",
            current_state="idle-no-work",
            claimed_work_item_id=None,
            last_successful_activity_at="2026-06-24T00:00:00Z",
            next_eligible_wake_at="2026-06-24T00:10:00Z",
            evidence_pointer="receipts/idle.md",
        )

    assert list((tmp_path / "heartbeats").iterdir()) == []


def test_claim_file_paths_are_collision_resistant_for_sanitized_work_item_ids(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.add_work_item({"work_item_id": "PR:36", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.add_work_item({"work_item_id": "PR_36", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.claim_work("PR:36", "lane-a", "session-a", "2026-06-24T00:00:00Z", 3600, "claims/colon.json")

    assert store.active_claim_for_work("PR_36", now="2026-06-24T00:01:00Z") is None
    store.claim_work("PR_36", "lane-b", "session-b", "2026-06-24T00:01:00Z", 3600, "claims/underscore.json")

    claim_files = sorted(path.name for path in (tmp_path / "claims").glob("*.json"))
    assert claim_files == ["PR%3A36.json", "PR_36.json"]


def test_stale_claims_can_be_recovered_with_evidence(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.add_work_item(
        {
            "work_item_id": "work-1",
            "repo_scope": "neoengine-ai-org/neowealth",
            "authorized_scopes": ["repo/app/page.tsx"],
            "status": "queued",
            "events": [{"event_type": "explicit_operator_command", "created_at": "2026-06-24T00:00:00Z"}],
            "created_at": "2026-06-24T00:00:00Z",
        }
    )
    store.claim_work("work-1", "lane-a", "session-old", "2026-06-24T00:00:00Z", 60, "claims/old.json")
    assert store.read_json("work/items.json")["work-1"]["status"] == "claimed"

    claim = store.claim_work(
        "work-1",
        "lane-a",
        "session-new",
        "2026-06-24T00:02:00Z",
        60,
        "claims/new.json",
        recovery_evidence_path="receipts/recovered-work-1.md",
    )

    assert claim["claim_owner_session_id"] == "session-new"
    assert claim["claim_status"] == "active"
    history = store.read_json("claims/history.json")
    assert history[-1]["claim_status"] == "expired/recovered"
    assert history[-1]["recovery_evidence_path"] == "receipts/recovered-work-1.md"
    items = store.read_json("work/items.json")
    assert items["work-1"]["status"] == "claimed"
    assert items["work-1"]["updated_at"]


def test_file_backed_direct_claim_requires_existing_non_governance_held_work(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)

    with pytest.raises(ValueError, match="work item does not exist"):
        store.claim_work("missing", "lane-a", "session-a", "2026-06-24T00:00:00Z", 3600, "claims/missing.json")

    store.add_work_item({"work_item_id": "held", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open", "labels": ["do-not-merge"]})
    with pytest.raises(ValueError, match="work item is not claimable: held_by_do_not_merge"):
        store.claim_work("held", "lane-a", "session-a", "2026-06-24T00:00:00Z", 3600, "claims/held.json")

    assert store.claim_for_work("missing") is None
    assert store.claim_for_work("held") is None


def test_file_backed_close_claim_refuses_duplicate_terminal_close(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.add_work_item({"work_item_id": "work-1", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.claim_work("work-1", "lane-a", "session-a", "2026-06-24T00:00:00Z", 3600, "claims/work-1.json")
    store.close_claim("work-1", "completed", "receipts/done.md", "2026-06-24T00:10:00Z")

    with pytest.raises(ValueError, match="claim is not active"):
        store.close_claim("work-1", "blocked", "receipts/blocked.md", "2026-06-24T00:11:00Z")

    assert store.read_json("work/items.json")["work-1"]["status"] == "completed"


def test_file_backed_close_claim_updates_work_item_status(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.add_work_item({"work_item_id": "work-1", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.claim_work("work-1", "lane-a", "session-a", "2026-06-24T00:00:00Z", 3600, "claims/work-1.json")

    store.close_claim("work-1", "completed", "receipts/done.md", "2026-06-24T00:10:00Z")

    assert store.read_json("work/items.json")["work-1"]["status"] == "completed"


def test_idle_lanes_back_off_rather_than_spin(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="neoengine-ai-org/neowealth", authorized_scopes=["repo/**"])

    pickup = store.pick_next_work("lane-a", "session-a", now="2026-06-24T00:00:00Z")

    assert pickup["action"] == "idle-no-work"
    hb = store.latest_heartbeat("lane-a")
    assert hb["current_state"] == "idle-no-work"
    assert utc_parse(hb["next_eligible_wake_at"]) > utc_parse("2026-06-24T00:00:00Z")


def test_event_wake_beats_timer_wake(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="neoengine-ai-org/neowealth", authorized_scopes=["repo/**"])
    store.emit_heartbeat(
        lane_id="lane-a",
        agent_session_id="session-a",
        repo_scope="neoengine-ai-org/neowealth",
        current_state="idle-no-work",
        claimed_work_item_id=None,
        last_successful_activity_at="2026-06-24T00:00:00Z",
        next_eligible_wake_at="2026-06-24T01:00:00Z",
        evidence_pointer="receipts/idle.md",
    )
    store.add_work_item(
        {
            "work_item_id": "work-1",
            "repo_scope": "neoengine-ai-org/neowealth",
            "authorized_scopes": ["repo/app/page.tsx"],
            "status": "queued",
            "events": [{"event_type": "failed_ci_transition", "created_at": "2026-06-24T00:05:00Z"}],
            "created_at": "2026-06-24T00:05:00Z",
        }
    )

    pickup = store.pick_next_work("lane-a", "session-a", now="2026-06-24T00:10:00Z")

    assert pickup["action"] == "claimed"
    assert pickup["wake_reason"] == "event:failed_ci_transition"
    assert pickup["work_item_id"] == "work-1"


def test_missing_next_eligible_wake_at_does_not_crash_pickup(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="neoengine-ai-org/neowealth", authorized_scopes=["repo/**"])
    store.emit_heartbeat(
        lane_id="lane-a",
        agent_session_id="session-a",
        repo_scope="neoengine-ai-org/neowealth",
        current_state="idle-no-work",
        claimed_work_item_id=None,
        last_successful_activity_at="2026-06-24T00:00:00Z",
        next_eligible_wake_at="2026-06-24T01:00:00Z",
        evidence_pointer="receipts/idle.md",
    )
    hb = store.latest_heartbeat("lane-a")
    assert hb is not None
    hb["next_eligible_wake_at"] = None
    store.write_json("heartbeats/lane-a.json", hb)
    store.add_work_item(
        {
            "work_item_id": "work-1",
            "repo_scope": "neoengine-ai-org/neowealth",
            "authorized_scopes": ["repo/app/page.tsx"],
            "status": "queued",
            "events": [],
            "created_at": "2026-06-24T00:05:00Z",
        }
    )

    pickup = store.pick_next_work("lane-a", "session-a", now="2026-06-24T00:10:00Z")

    assert pickup["action"] == "claimed"
    assert pickup["wake_reason"] == "timer:fallback"


def test_blocked_work_is_not_repeatedly_repicked_without_new_event(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="neoengine-ai-org/neowealth", authorized_scopes=["repo/**"])
    store.add_work_item(
        {
            "work_item_id": "work-1",
            "repo_scope": "neoengine-ai-org/neowealth",
            "authorized_scopes": ["repo/app/page.tsx"],
            "status": "blocked",
            "blocked_at_event_id": "evt-1",
            "events": [{"event_id": "evt-1", "event_type": "failed_ci_transition", "created_at": "2026-06-24T00:00:00Z"}],
            "created_at": "2026-06-24T00:00:00Z",
        }
    )

    assert store.pick_next_work("lane-a", "session-a", now="2026-06-24T00:10:00Z")["action"] == "idle-no-work"

    store.record_event("work-1", "governance_unblock", "2026-06-24T00:11:00Z", event_id="evt-2")
    pickup = store.pick_next_work("lane-a", "session-a", now="2026-06-24T00:12:00Z")

    assert pickup["action"] == "claimed"
    assert pickup["work_item_id"] == "work-1"
    assert pickup["wake_reason"] == "event:governance_unblock"


def test_status_report_classifies_lane_states(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("working", repo_scope="r", authorized_scopes=["**"])
    store.register_lane("idle", repo_scope="r", authorized_scopes=["**"])
    store.register_lane("blocked", repo_scope="r", authorized_scopes=["**"])
    store.register_lane("human", repo_scope="r", authorized_scopes=["**"])
    store.register_lane("failed", repo_scope="r", authorized_scopes=["**"])
    store.emit_heartbeat("working", "s", "r", "working", "w", "2026-06-24T00:00:00Z", "2026-06-24T00:10:00Z", "e")
    store.add_work_item({"work_item_id": "w", "repo_scope": "r", "authorized_scopes": ["**"], "status": "open"})
    store.claim_work("w", "working", "s", "2026-06-24T00:00:00Z", 10800, "claims/w.json")
    store.emit_heartbeat("idle", "s", "r", "idle-no-work", None, "2026-06-24T00:00:00Z", "2026-06-24T01:00:00Z", "e")
    store.emit_heartbeat("blocked", "s", "r", "blocked", None, "2026-06-24T00:00:00Z", "2026-06-24T00:20:00Z", "e")
    store.emit_heartbeat("human", "s", "r", "awaiting-human", None, "2026-06-24T00:00:00Z", "2026-06-24T00:20:00Z", "e")
    store.emit_heartbeat("failed", "s", "r", "working", None, "2026-06-23T00:00:00Z", "2026-06-23T00:20:00Z", "e")
    failed_hb = store.latest_heartbeat("failed")
    assert failed_hb is not None
    failed_hb["emitted_at"] = "2026-06-23T00:00:00Z"
    store.write_json("heartbeats/failed.json", failed_hb)

    report = store.status_report(now="2026-06-24T02:00:00Z", stale_after_seconds=3600)
    by_lane = {row["lane_id"]: row for row in report["lanes"]}

    assert by_lane["working"]["status"] == "working"
    assert by_lane["idle"]["status"] == "idle-no-work"
    assert by_lane["blocked"]["status"] == "blocked"
    assert by_lane["human"]["status"] == "awaiting-human"
    assert by_lane["failed"]["status"] == "failed-heartbeat"
    assert by_lane["working"]["active_claims"] == ["w"]


def test_governance_bypass_cases_are_preserved(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="neoengine-ai-org/neowealth", authorized_scopes=["repo/**"])
    for work_id, field in [
        ("held", {"labels": ["do-not-merge"]}),
        ("human", {"awaiting_human_review": True}),
        ("refused", {"status": "refused", "sidecar_evidence_path": None}),
    ]:
        item = {
            "work_item_id": work_id,
            "repo_scope": "neoengine-ai-org/neowealth",
            "authorized_scopes": ["repo/app/page.tsx"],
            "status": "queued",
            "events": [{"event_type": "explicit_operator_command", "created_at": "2026-06-24T00:00:00Z"}],
            "created_at": "2026-06-24T00:00:00Z",
        }
        item.update(field)
        store.add_work_item(item)

    pickup = store.pick_next_work("lane-a", "session-a", now="2026-06-24T00:01:00Z")

    assert pickup["action"] == "idle-no-work"
    skipped = store.read_json("receipts/skips.json")
    reasons = {row["work_item_id"]: row["reason"] for row in skipped}
    assert reasons == {
        "held": "held_by_do_not_merge",
        "human": "awaiting_human_review",
        "refused": "refused_without_sidecar_evidence",
    }


def _assignment_item(work_item_id="assigned", **overrides):
    return {
        "work_item_id": work_item_id,
        "repo_scope": "repo",
        "authorized_scopes": ["**"],
        "status": "open",
        "events": [{"event_id": "evt-1", "event_type": "explicit_operator_command", "created_at": "2026-06-24T00:00:00Z"}],
        **overrides,
    }


def test_required_lane_blocks_wildcard_picker_and_direct_claim(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("intended", repo_scope="repo", authorized_scopes=["**"])
    store.register_lane("wildcard", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item(_assignment_item(required_lane_id="intended"))

    assert store.pick_next_work("wildcard", "other", now="2026-06-24T00:01:00Z")["action"] == "idle-no-work"
    with pytest.raises(ValueError, match="outside_required_lane"):
        store.claim_work("assigned", "wildcard", "other", "2026-06-24T00:01:00Z", 60, "receipts/other")
    assert store.claim_for_work("assigned") is None
    assert store.pick_next_work("intended", "owner", now="2026-06-24T00:01:00Z")["work_item_id"] == "assigned"


def test_required_work_item_blocks_legacy_wildcard_work(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("intended", repo_scope="repo", authorized_scopes=["**"])
    # A persisted lane record is also read by older processes. Exercise the
    # consumer directly instead of relying on the new registration argument.
    lanes = store.read_json("lanes.json", {})
    lanes["intended"]["required_work_item_id"] = "assigned"
    store.write_json("lanes.json", lanes)
    store.add_work_item(_assignment_item("legacy", operator_priority=1))

    assert store.pick_next_work("intended", "owner", now="2026-06-24T00:01:00Z")["action"] == "idle-no-work"
    with pytest.raises(ValueError, match="outside_required_work_item"):
        store.claim_work("legacy", "intended", "owner", "2026-06-24T00:01:00Z", 60, "receipts/legacy")
    store.add_work_item(_assignment_item(required_lane_id="intended", operator_priority=100))
    assert store.pick_next_work("intended", "owner", now="2026-06-24T00:01:00Z")["work_item_id"] == "assigned"
    assert store.claim_for_work("legacy") is None


def test_close_blocked_binds_current_event_until_new_event(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item(_assignment_item())
    store.claim_work("assigned", "lane-a", "owner", "2026-06-24T00:00:00Z", 3600, "receipts/start")
    store.record_event("assigned", "review_request", "2026-06-24T00:01:00Z", event_id="evt-2")
    store.close_claim("assigned", "blocked", "receipts/blocker", "2026-06-24T00:02:00Z")

    item = store.read_json("work/items.json")["assigned"]
    assert item["status"] == "blocked"
    assert item["blocked_at_event_id"] == "evt-2"
    assert store.pick_next_work("lane-a", "next", now="2026-06-24T00:03:00Z")["action"] == "idle-no-work"
    assert len(store.read_json("claims/history.json")) == 1
    store.record_event("assigned", "governance_unblock", "2026-06-24T00:04:00Z", event_id="evt-3")
    pickup = store.pick_next_work("lane-a", "next", now="2026-06-24T00:05:00Z")
    assert pickup["action"] == "claimed"
    assert pickup["wake_reason"] == "event:governance_unblock"


def test_competing_claims_have_one_owner_after_delayed_publication(tmp_path: Path, monkeypatch) -> None:
    import threading

    first = DevLaneStore(tmp_path)
    second = DevLaneStore(tmp_path)
    first.add_work_item(_assignment_item())
    reached_write = threading.Event()
    release_first = threading.Event()
    second_reached_write = threading.Event()
    second_finished = threading.Event()
    original_write = DevLaneStore.write_json
    results = {}

    def delayed_write(self, relative, value):
        if str(relative) == "claims/assigned.json" and value.get("claim_status") == "active":
            if value["claim_owner_session_id"] == "first":
                reached_write.set()
                if not release_first.wait(5):
                    raise RuntimeError("test did not release first claimant")
            else:
                second_reached_write.set()
        return original_write(self, relative, value)

    def claim(store, owner):
        try:
            results[owner] = store.claim_work("assigned", owner, owner, "2026-06-24T00:00:00Z", 3600, "receipts/" + owner)
        except Exception as exc:
            results[owner] = exc
        finally:
            if owner == "second":
                second_finished.set()

    monkeypatch.setattr(DevLaneStore, "write_json", delayed_write)
    a = threading.Thread(target=claim, args=(first, "first"))
    b = threading.Thread(target=claim, args=(second, "second"))
    a.start()
    try:
        assert reached_write.wait(5)
        b.start()
        if second_reached_write.wait(0.3):
            assert second_finished.wait(5)
    finally:
        release_first.set()
        a.join(5)
        if b.ident is not None:
            b.join(5)
    assert not a.is_alive() and not b.is_alive()
    assert isinstance(results["first"], dict)
    assert isinstance(results["second"], ValueError)
    assert "active claim already exists" in str(results["second"])
    assert first.claim_for_work("assigned")["claim_owner_session_id"] == "first"
    assert first.read_json("claims/history.json", []) == []


@pytest.mark.parametrize("field,bad", [
    ("required_lane_id", None), ("required_lane_id", ""), ("required_lane_id", []), ("required_lane_id", 7),
    ("required_work_item_id", None), ("required_work_item_id", ""), ("required_work_item_id", []), ("required_work_item_id", 7),
])
def test_malformed_persisted_assignments_refuse_without_claim(tmp_path: Path, field, bad) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    item = _assignment_item()
    if field == "required_lane_id":
        item[field] = bad
    else:
        lanes = store.read_json("lanes.json")
        lanes["lane-a"][field] = bad
        store.write_json("lanes.json", lanes)
    store.add_work_item(item)
    assert store.pick_next_work("lane-a", "owner", now="2026-06-24T00:00:00Z")["action"] == "idle-no-work"
    with pytest.raises(ValueError, match="invalid_required_"):
        store.claim_work("assigned", "lane-a", "owner", "2026-06-24T00:00:00Z", 60, "receipts/start")
    assert store.claim_for_work("assigned") is None


def test_explicit_assignment_requires_real_registered_lane(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.add_work_item(_assignment_item(required_lane_id="missing"))
    with pytest.raises(ValueError, match="required_lane_not_registered"):
        store.claim_work("assigned", "missing", "owner", "2026-06-24T00:00:00Z", 60, "receipts/start")
    assert store.claim_for_work("assigned") is None


def test_wrong_assignment_does_not_mutate_expired_owner_or_history(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("intended", repo_scope="repo", authorized_scopes=["**"], required_work_item_id="assigned")
    store.register_lane("other", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item(_assignment_item(required_lane_id="intended"))
    store.claim_work("assigned", "intended", "old", "2026-06-24T00:00:00Z", 60, "receipts/old")
    before_claim = (tmp_path / "claims/assigned.json").read_bytes()
    before_items = (tmp_path / "work/items.json").read_bytes()
    with pytest.raises(ValueError, match="outside_required_lane"):
        store.claim_work("assigned", "other", "new", "2026-06-24T00:02:00Z", 60, "receipts/new", recovery_evidence_path="receipts/recovery")
    assert (tmp_path / "claims/assigned.json").read_bytes() == before_claim
    assert (tmp_path / "work/items.json").read_bytes() == before_items
    assert store.read_json("claims/history.json", []) == []
    recovered = store.claim_work("assigned", "intended", "new", "2026-06-24T00:02:00Z", 60, "receipts/new", recovery_evidence_path="receipts/recovery")
    assert recovered["claim_owner_session_id"] == "new"
    assert len(store.read_json("claims/history.json")) == 1


def test_claim_rechecks_assignment_changed_after_pick_selection(tmp_path: Path, monkeypatch) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item(_assignment_item())
    original_claim = store.claim_work

    def change_assignment_then_claim(*args, **kwargs):
        store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"], required_work_item_id="different")
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(store, "claim_work", change_assignment_then_claim)
    with pytest.raises(ValueError, match="outside_required_work_item"):
        store.pick_next_work("lane-a", "owner", now="2026-06-24T00:00:00Z")
    assert store.claim_for_work("assigned") is None


def test_lane_registration_preserves_assignment_when_legacy_caller_omits_it(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    original = store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"], required_work_item_id="assigned")
    repeated = store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    assert repeated == original


@pytest.mark.parametrize("bad", ["", [], 7])
def test_invalid_registration_assignment_does_not_change_registry(tmp_path: Path, bad) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    before = (tmp_path / "lanes.json").read_bytes()
    with pytest.raises(ValueError, match="invalid required work-item assignment"):
        store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"], required_work_item_id=bad)
    assert (tmp_path / "lanes.json").read_bytes() == before


@pytest.mark.parametrize("restriction,reason", [
    ({"labels": ["do-not-merge"]}, "held_by_do_not_merge"),
    ({"awaiting_human_review": True}, "awaiting_human_review"),
    ({"status": "refused"}, "refused_without_sidecar_evidence"),
    ({"repo_scope": "another-repo"}, "outside_repo_scope"),
    ({"authorized_scopes": ["outside/**"]}, "outside_lane_capability"),
])
def test_assignment_does_not_override_existing_admission_guards(tmp_path: Path, restriction, reason) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["docs/**"], required_work_item_id="assigned")
    store.add_work_item(_assignment_item(**({"required_lane_id": "lane-a", "authorized_scopes": ["docs/proof.md"]} | restriction)))
    with pytest.raises(ValueError, match=reason):
        store.claim_work("assigned", "lane-a", "owner", "2026-06-24T00:00:00Z", 60, "receipts/start")
    assert store.claim_for_work("assigned") is None


def test_exact_owner_closeout_refuses_replacement_owner(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.add_work_item(_assignment_item())
    store.claim_work("assigned", "lane-a", "original", "2026-06-24T00:00:00Z", 60, "receipts/original")
    store.claim_work("assigned", "lane-a", "replacement", "2026-06-24T00:02:00Z", 60, "receipts/replacement", recovery_evidence_path="receipts/recovery")
    before = {p: (tmp_path / p).read_bytes() for p in ("claims/assigned.json", "claims/history.json", "work/items.json")}
    with pytest.raises(ValueError, match="claim owner changed"):
        store.close_claim("assigned", "blocked", "receipts/old", "2026-06-24T00:02:01Z", expected_owner_session_id="original")
    assert all((tmp_path / p).read_bytes() == raw for p, raw in before.items())
    closed = store.close_claim("assigned", "blocked", "receipts/current", "2026-06-24T00:02:01Z", expected_owner_session_id="replacement")
    assert closed["claim_owner_session_id"] == "replacement"
    assert store.read_json("work/items.json")["assigned"]["blocked_at_event_id"] == "evt-1"


@pytest.mark.parametrize("status", ["completed", "refused", "superseded", "no-op with evidence", "expired/recovered"])
def test_nonblocked_closeout_does_not_invent_blocked_event(tmp_path: Path, status) -> None:
    store = DevLaneStore(tmp_path)
    store.add_work_item(_assignment_item())
    store.claim_work("assigned", "lane-a", "owner", "2026-06-24T00:00:00Z", 3600, "receipts/start")
    store.close_claim("assigned", status, "receipts/end", "2026-06-24T00:01:00Z", expected_owner_session_id="owner")
    assert "blocked_at_event_id" not in store.read_json("work/items.json")["assigned"]


def test_cli_registration_accepts_explicit_work_item(tmp_path: Path, capsys) -> None:
    from neoengine_local.dev_lane_heartbeat import main

    assert main(["--root", str(tmp_path), "register-lane", "lane-a", "--repo-scope", "repo", "--authorized-scope", "docs/**", "--required-work-item-id", "assigned"]) == 0
    assert json.loads(capsys.readouterr().out)["required_work_item_id"] == "assigned"


def test_registered_assignment_refuses_missing_item_before_expired_recovery(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item(_assignment_item("old-work"))
    store.claim_work("old-work", "lane-a", "old-owner", "2026-06-24T00:00:00Z", 60, "receipts/old")
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"], required_work_item_id="new-work")
    # Model a retained claim whose work item is no longer in the producer's
    # current registry. The claim is real; missing state must not authorize
    # recovery for a lane now bound to a different item.
    store.write_json("work/items.json", {})
    before_claim = (tmp_path / "claims/old-work.json").read_bytes()
    before_items = (tmp_path / "work/items.json").read_bytes()

    with pytest.raises(ValueError) as error:
        store.claim_work("old-work", "lane-a", "new-owner", "2026-06-24T00:02:00Z", 60, "receipts/new", recovery_evidence_path="receipts/recovery")

    assert store.read_json("claims/history.json", []) == []
    assert (tmp_path / "claims/old-work.json").read_bytes() == before_claim
    assert (tmp_path / "work/items.json").read_bytes() == before_items
    assert "outside_required_work_item" in str(error.value)
