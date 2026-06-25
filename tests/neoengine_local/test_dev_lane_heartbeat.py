from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# Spawned multiprocessing workers can inherit sys.path entries inserted by
# deployed-script integration tests.  Keep this checkout first so workers test
# the PR code, not an older live deployment checkout.
try:
    sys.path.remove(str(REPO_ROOT))
except ValueError:
    pass
sys.path.insert(0, str(REPO_ROOT))

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

    with pytest.raises(ValueError, match="unsafe|reserved|stable safe slug"):
        store.add_work_item({"work_item_id": work_item_id, "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})

    assert store.read_json("claims/history.json") == original_history


def test_legacy_active_claim_is_visible_and_closes_without_leaving_active_copy(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item({"work_item_id": "work-1", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    legacy_claim = {
        "schema_version": "dev_lane_claim.v1",
        "work_item_id": "work-1",
        "lane_id": "lane-a",
        "claim_owner_session_id": "session-old",
        "claim_started_at": "2026-06-24T00:00:00Z",
        "claim_expires_at": "2026-06-24T01:00:00Z",
        "claim_status": "active",
        "claim_evidence_path": "claims/work-1.json",
    }
    store.write_json("claims/work-1.json", legacy_claim)

    assert store.active_claim_for_work("work-1", now="2026-06-24T00:10:00Z") is not None
    closed = store.close_claim("work-1", "completed", "receipts/done.md", "2026-06-24T00:20:00Z")
    report = store.status_report(now="2026-06-24T00:21:00Z")

    assert closed["claim_status"] == "completed"
    assert store.read_json("claims/work-1.json")["claim_status"] == "completed"
    assert report["lanes"][0]["active_claims"] == []


def test_legacy_percent_encoded_claim_can_be_read_without_reopening_unsafe_id_admission(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    legacy_claim = {
        "schema_version": "dev_lane_claim.v1",
        "work_item_id": "PR:36",
        "lane_id": "lane-a",
        "claim_owner_session_id": "session-old",
        "claim_started_at": "2026-06-24T00:00:00Z",
        "claim_expires_at": "2026-06-24T01:00:00Z",
        "claim_status": "active",
        "claim_evidence_path": "claims/PR%3A36.json",
    }
    store.write_json("claims/PR%3A36.json", legacy_claim)

    assert store.claim_for_work("PR:36") == legacy_claim
    assert store.active_claim_for_work("PR:36", now="2026-06-24T00:10:00Z") == legacy_claim
    store.add_work_item({"work_item_id": "PR:36", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    assert (tmp_path / "work" / "items.json").exists()


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


def test_canonical_terminal_does_not_hide_legacy_active_claim(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.register_lane("lane-b", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item({"work_item_id": "work-2", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    legacy_active = {
        "schema_version": "dev_lane_claim.v1",
        "work_item_id": "work-2",
        "lane_id": "lane-a",
        "claim_owner_session_id": "old",
        "claim_started_at": "2026-06-24T00:00:00Z",
        "claim_expires_at": "2026-06-24T01:00:00Z",
        "claim_status": "active",
        "claim_evidence_path": "claims/work-2.json",
    }
    canonical_closed = {**legacy_active, "claim_status": "completed", "closed_at": "2026-06-24T00:10:00Z"}
    store.write_json("claims/by-work-item/work-2.json", canonical_closed)
    store.write_json("claims/work-2.json", legacy_active)

    assert store.active_claim_for_work("work-2", now="2026-06-24T00:20:00Z") == legacy_active
    with pytest.raises(ValueError, match="active claim already exists"):
        store.claim_work("work-2", "lane-b", "new", "2026-06-24T00:20:00Z", 3600, "evidence")


def _file_backed_claim_worker(args: tuple[str, str]) -> dict[str, str]:
    root, lane_id = args
    store = DevLaneStore(Path(root))
    try:
        claim = store.claim_work(
            "work-concurrent",
            lane_id,
            f"session-{lane_id}",
            "2099-06-24T00:00:00Z",
            3600,
            f"claims/{lane_id}.json",
        )
        return {"lane_id": lane_id, "claimed": "true", "claim_owner_session_id": claim["claim_owner_session_id"]}
    except ValueError as exc:
        return {"lane_id": lane_id, "claimed": "false", "error": str(exc)}


def test_file_backed_blocked_closeout_baselines_event_across_lanes(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.register_lane("lane-b", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item({"work_item_id": "work-blocked", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    event = store.record_event("work-blocked", "failed_ci_transition", "2026-06-24T00:00:00Z", event_id="e1")

    first = store.pick_next_work("lane-a", "session-a", now="2026-06-24T00:01:00Z")
    assert first["action"] == "claimed"
    store.close_claim("work-blocked", "blocked", "receipts/blocked.md", "2026-06-24T00:02:00Z")

    item = store.read_json("work/items.json")["work-blocked"]
    assert item["status"] == "blocked"
    assert item["blocked_at_event_id"] == event["event_id"]

    second = store.pick_next_work("lane-b", "session-b", now="2026-06-24T00:03:00Z")
    assert second["action"] == "idle-no-work"


def test_file_backed_claim_work_is_serialized_across_processes(tmp_path: Path) -> None:
    import multiprocessing as mp

    store = DevLaneStore(tmp_path)
    store.add_work_item({"work_item_id": "work-concurrent", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=4) as pool:
        results = pool.map(_file_backed_claim_worker, [(str(tmp_path), f"lane-{idx}") for idx in range(4)])

    assert sum(1 for result in results if result["claimed"] == "true") == 1
    assert sum(1 for result in results if "active claim already exists" in result.get("error", "")) == 3
    claims = [path for path in (tmp_path / "claims" / "by-work-item").glob("*.json")]
    assert len(claims) == 1


def test_colon_work_item_id_uses_encoded_non_colliding_claim_path(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    work_item_id = "neowealth:startup-proof:codex"
    store.add_work_item({"work_item_id": work_item_id, "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    claim = store.claim_work(work_item_id, "lane-a", "session-a", "2026-06-24T00:00:00Z", 3600, "claims/colon.json")

    assert claim["work_item_id"] == work_item_id
    assert store.active_claim_for_work(work_item_id, now="2026-06-24T00:01:00Z") is not None
    claim_files = sorted(path.name for path in (tmp_path / "claims" / "by-work-item").glob("*.json"))
    assert claim_files == ["neowealth%3Astartup-proof%3Acodex.json"]
    assert not (tmp_path / "claims" / "history.json").exists()


def test_file_backed_refresh_preserves_active_claimed_status_and_terminal_closeout(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.add_work_item({"work_item_id": "work-refresh-active", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.claim_work("work-refresh-active", "lane-a", "session-a", "2099-06-24T00:00:00Z", 3600, "claims/a.json")

    refreshed = store.add_work_item({"work_item_id": "work-refresh-active", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "queued"})
    status = store.status_report(now="2099-06-24T00:05:00Z")
    store.close_claim("work-refresh-active", "completed", "receipts/done.md", "2099-06-24T00:06:00Z")
    closed = store.read_json("work/items.json")["work-refresh-active"]

    assert refreshed["status"] == "claimed"
    assert status["lanes"] == [] or "work-refresh-active" not in [item.get("work_item_id") for item in status["lanes"]]
    assert closed["status"] == "completed"


def test_file_backed_blocked_baseline_survives_refresh_until_new_valid_event(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item({"work_item_id": "work-blocked-refresh", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "blocked"})
    e1 = store.record_event("work-blocked-refresh", "new_repair_packet", "2026-06-24T00:00:00Z", event_id="E1")
    store.claim_work("work-blocked-refresh", "lane-a", "session-a", "2026-06-24T00:01:00Z", 3600, "claims/a.json")
    store.close_claim("work-blocked-refresh", "blocked", "receipts/blocked.md", "2026-06-24T00:02:00Z")

    refreshed = store.add_work_item({"work_item_id": "work-blocked-refresh", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "blocked"})
    denied = store.pick_next_work("lane-a", "session-b", now="2026-06-24T00:03:00Z")
    e2 = store.record_event("work-blocked-refresh", "governance_unblock", "2026-06-24T00:04:00Z", event_id="E2")
    picked = store.pick_next_work("lane-a", "session-c", now="2026-06-24T00:05:00Z")

    assert e1["event_id"] == "E1"
    assert refreshed["blocked_at_event_id"] == "E1"
    assert denied["action"] == "idle-no-work"
    assert e2["event_id"] == "E2"
    assert picked["action"] == "claimed"
    assert picked["claim"]["claimed_event_id"] == "E2"


def test_file_backed_claim_consumes_prior_same_work_item_events_only(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.register_lane("lane-b", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item({"work_item_id": "work-events", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.add_work_item({"work_item_id": "work-other", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.record_event("work-events", "new_repair_packet", "2026-06-24T00:00:00Z", event_id="E1")
    store.record_event("work-other", "new_repair_packet", "2026-06-24T00:01:00Z", event_id="O1")
    store.record_event("work-events", "followup_repair_packet", "2026-06-24T00:02:00Z", event_id="E2")

    claim = store.claim_work("work-events", "lane-a", "session-a", "2026-06-24T00:03:00Z", 3600, "claims/a.json")
    store.record_event("work-events", "governance_unblock", "2026-06-24T00:04:00Z", event_id="E3")
    items = store.read_json("work/items.json")
    wake = store.pick_next_work("lane-b", "session-b", now="2026-06-24T00:05:00Z")

    event_map = {event["event_id"]: event for event in items["work-events"]["events"]}
    assert claim["claimed_event_id"] == "E2"
    assert event_map["E1"]["consumed_at"] == "2026-06-24T00:03:00Z"
    assert event_map["E2"]["consumed_at"] == "2026-06-24T00:03:00Z"
    assert "consumed_at" not in event_map["E3"]
    assert wake["action"] == "claimed"
    assert wake["work_item_id"] == "work-other"


def test_event_vocabulary_matches_sqlite_backend() -> None:
    from hermes_cli import kanban_db as kb
    from neoengine_local import dev_lane_heartbeat as hb

    assert kb.LANE_VALID_WAKE_EVENTS == hb.VALID_WAKE_EVENTS


def test_file_backed_scanner_rejects_invalid_event_without_unblocking_blocked_item(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item({
        "work_item_id": "work-invalid-scanner-event",
        "repo_scope": "repo",
        "authorized_scopes": ["**"],
        "status": "blocked",
        "blocked_at_event_id": "E1",
        "events": [{"event_id": "E1", "event_type": "new_repair_packet", "created_at": "2099-06-24T00:00:00Z"}],
    })

    initial = store.pick_next_work("lane-a", "session-a", now="2099-06-24T00:01:00Z")
    with pytest.raises(ValueError, match="invalid wake event"):
        store.add_work_item({
            "work_item_id": "work-invalid-scanner-event",
            "repo_scope": "repo",
            "authorized_scopes": ["**"],
            "status": "blocked",
            "events": [{"event_id": "BAD", "event_type": "unauthorized_closeout", "created_at": "2099-06-24T00:02:00Z"}],
        })
    after_invalid = store.pick_next_work("lane-a", "session-b", now="2099-06-24T00:03:00Z")
    item = store.read_json("work/items.json")["work-invalid-scanner-event"]

    assert initial["action"] == "idle-no-work"
    assert after_invalid["action"] == "idle-no-work"
    assert [event["event_id"] for event in item["events"]] == ["E1"]
    assert item["blocked_at_event_id"] == "E1"


def test_file_backed_same_timestamp_followup_event_survives_blocked_closeout(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.register_lane("lane-b", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item({"work_item_id": "work-same-ts", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.record_event("work-same-ts", "new_repair_packet", "2099-06-24T00:00:00Z", event_id="E1")
    first = store.pick_next_work("lane-a", "session-a", now="2099-06-24T00:01:00Z")
    store.record_event("work-same-ts", "followup_repair_packet", "2099-06-24T00:00:00Z", event_id="E2")
    store.close_claim("work-same-ts", "blocked", "receipts/blocked.md", "2099-06-24T00:02:00Z")
    second = store.pick_next_work("lane-b", "session-b", now="2099-06-24T00:03:00Z")

    assert first["claim"]["claimed_event_id"] == "E1"
    assert second["action"] == "claimed"
    assert second["claim"]["claimed_event_id"] == "E2"


def test_file_backed_legacy_claim_recovery_closes_legacy_path(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-old", repo_scope="repo", authorized_scopes=["**"])
    store.register_lane("lane-new", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item({"work_item_id": "work-legacy-recovery", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    legacy_claim = {
        "work_item_id": "work-legacy-recovery",
        "lane_id": "lane-old",
        "claim_owner_session_id": "session-old",
        "claim_status": "active",
        "claimed_at": "2099-06-24T00:00:00Z",
        "claim_expires_at": "2099-06-24T00:01:00Z",
    }
    store.write_json("claims/work-legacy-recovery.json", legacy_claim)

    new_claim = store.claim_work(
        "work-legacy-recovery",
        "lane-new",
        "session-new",
        "2099-06-24T00:02:00Z",
        3600,
        "claims/new.json",
        recovery_evidence_path="receipts/recovered.md",
    )
    report = store.status_report(now="2099-06-24T00:03:00Z")
    legacy_after = store.read_json("claims/work-legacy-recovery.json")

    assert new_claim["claim_status"] == "active"
    assert legacy_after["claim_status"] == "expired/recovered"
    rows = {row["lane_id"]: row for row in report["lanes"]}
    assert rows["lane-new"]["active_claims"] == ["work-legacy-recovery"]
    assert rows["lane-old"]["stale_claims"] == []


def test_file_backed_blocked_item_without_event_list_fails_closed(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item({
        "work_item_id": "work-missing-events",
        "repo_scope": "repo",
        "authorized_scopes": ["**"],
        "status": "blocked",
        "blocked_at_event_id": "E1",
    })

    result = store.pick_next_work("lane-a", "session-a", now="2099-06-24T00:00:00Z")

    assert result["action"] == "idle-no-work"


def test_file_backed_blocked_refresh_ignores_older_valid_injected_event(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item({"work_item_id": "work-old-event", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.record_event("work-old-event", "failed_ci_transition", "2099-06-24T00:10:00Z", event_id="E1")
    first = store.pick_next_work("lane-a", "session-a", now="2099-06-24T00:11:00Z")
    store.close_claim("work-old-event", "blocked", "receipts/blocked.md", "2099-06-24T00:12:00Z")

    refreshed = store.add_work_item({
        "work_item_id": "work-old-event",
        "repo_scope": "repo",
        "authorized_scopes": ["**"],
        "status": "blocked",
        "events": [{"event_id": "OLD", "event_type": "governance_unblock", "created_at": "2099-06-24T00:00:00Z"}],
    })
    second = store.pick_next_work("lane-a", "session-b", now="2099-06-24T00:13:00Z")

    assert first["claim"]["claimed_event_id"] == "E1"
    assert [event["event_id"] for event in refreshed["events"]] == ["E1"]
    assert second["action"] == "idle-no-work"


def test_file_backed_pickup_evidence_path_points_to_canonical_claim(tmp_path: Path) -> None:
    store = DevLaneStore(tmp_path)
    store.register_lane("lane-a", repo_scope="repo", authorized_scopes=["**"])
    store.add_work_item({"work_item_id": "work-evidence", "repo_scope": "repo", "authorized_scopes": ["**"], "status": "open"})
    store.record_event("work-evidence", "new_repair_packet", "2099-06-24T00:00:00Z", event_id="E1")

    result = store.pick_next_work("lane-a", "session-a", now="2099-06-24T00:01:00Z")
    heartbeat = store.latest_heartbeat("lane-a")

    assert result["claim"]["claim_evidence_path"] == "claims/by-work-item/work-evidence.json"
    assert heartbeat["evidence_pointer"] == "claims/by-work-item/work-evidence.json"
    assert (tmp_path / heartbeat["evidence_pointer"]).exists()
