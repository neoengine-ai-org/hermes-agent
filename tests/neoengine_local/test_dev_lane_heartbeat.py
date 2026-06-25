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

    with pytest.raises(ValueError, match="unsafe|reserved|stable safe slug"):
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


def test_claim_file_paths_use_non_colliding_namespace_for_valid_ids(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.add_work_item({"work_item_id": "PR-36", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.add_work_item({"work_item_id": "PR_36", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.claim_work("PR-36", "lane-a", "session-a", "2026-06-24T00:00:00Z", 3600, "claims/dash.json")

    assert store.active_claim_for_work("PR_36", now="2026-06-24T00:01:00Z") is None
    store.claim_work("PR_36", "lane-b", "session-b", "2026-06-24T00:01:00Z", 3600, "claims/underscore.json")

    claim_files = sorted(path.name for path in (tmp_path / "claims" / "by-work-item").glob("*.json"))
    assert claim_files == ["PR-36.json", "PR_36.json"]



@pytest.mark.parametrize("work_item_id", ["../x", "a/b", "/abs", "claims", "history.json", "caf\u0301"])
def test_unsafe_work_item_ids_are_rejected_without_mutating_history(tmp_path: Path, work_item_id: str) -> None:
    store = DevLaneStore(tmp_path)
    original_history = [{"work_item_id": "prior", "claim_status": "completed"}]
    store.write_json("claims/history.json", original_history)
    store.add_work_item({"work_item_id": work_item_id, "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})

    with pytest.raises(ValueError, match="unsafe|reserved|stable safe slug"):
        store.claim_work(work_item_id, "lane-a", "session-a", "2026-06-24T00:00:00Z", 3600, "claims/x.json")

    assert store.read_json("claims/history.json") == original_history


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
