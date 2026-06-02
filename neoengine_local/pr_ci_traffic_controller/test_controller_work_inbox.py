#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess

HELPER_PATH = pathlib.Path(__file__).with_name("controller_work_inbox.py")
spec = importlib.util.spec_from_file_location("controller_work_inbox", HELPER_PATH)
mod = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(mod)  # type: ignore[union-attr]


def test_watchdog_creates_controller_inbox_item_for_owned_blocker(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "neoengine-controller-work-inbox.json"
    item = mod.upsert_work_item(
        path,
        org="neoengine",
        repo="neoengine-ai-org/neoengine",
        producer="hourly_watchdog",
        work_type="branch_namespace_collision",
        priority="high",
        state_fingerprint="collision:refs/heads/a|refs/heads/a/b",
        blocker="dispatcher cannot launch because local branch namespace collides",
        exact_instruction="inspect local refs safely; archive only if safe",
        authority_required="tier1_local",
    )
    data = json.loads(path.read_text())
    assert data["schema"] == mod.SCHEMA
    assert data["org"] == "neoengine"
    assert data["items"][0]["work_item_id"] == item["work_item_id"]
    assert data["items"][0]["consumer"] == "pr_ci_controller"
    assert data["items"][0]["status"] == "queued"


def test_duplicate_watchdog_observation_updates_existing_item(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "neoengine-controller-work-inbox.json"
    first = mod.upsert_work_item(path, org="neoengine", repo="neoengine-ai-org/neoengine", producer="hourly_watchdog", work_type="ci_pressure_recheck", priority="medium", state_fingerprint="same", blocker="old", exact_instruction="old", authority_required="tier1_local")
    second = mod.upsert_work_item(path, org="neoengine", repo="neoengine-ai-org/neoengine", producer="hourly_watchdog", work_type="ci_pressure_recheck", priority="high", state_fingerprint="same", blocker="new", exact_instruction="new", authority_required="tier1_local")
    data = json.loads(path.read_text())
    assert len(data["items"]) == 1
    assert first["work_item_id"] == second["work_item_id"]
    assert data["items"][0]["priority"] == "high"
    assert data["items"][0]["blocker"] == "new"


def test_completed_item_not_reopened_unless_fingerprint_changes(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "neoengine-controller-work-inbox.json"
    item = mod.upsert_work_item(path, org="neoengine", repo="neoengine-ai-org/neoengine", producer="hourly_watchdog", work_type="ci_pressure_recheck", priority="medium", state_fingerprint="fp1", blocker="x", exact_instruction="x", authority_required="tier1_local")
    data = json.loads(path.read_text())
    data["items"][0]["status"] = "done"
    data["items"][0]["completion_receipt"] = {"completed_at": "synthetic", "outcome": "done"}
    mod.atomic_write_json(path, data)
    same = mod.upsert_work_item(path, org="neoengine", repo="neoengine-ai-org/neoengine", producer="hourly_watchdog", work_type="ci_pressure_recheck", priority="medium", state_fingerprint="fp1", blocker="x", exact_instruction="x", authority_required="tier1_local")
    changed = mod.upsert_work_item(path, org="neoengine", repo="neoengine-ai-org/neoengine", producer="hourly_watchdog", work_type="ci_pressure_recheck", priority="medium", state_fingerprint="fp2", blocker="x", exact_instruction="x", authority_required="tier1_local")
    data = json.loads(path.read_text())
    assert same["status"] == "done"
    assert len(data["items"]) == 2
    assert changed["status"] == "queued"


def test_malformed_inbox_fails_closed(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "neoengine-controller-work-inbox.json"
    path.write_text("not json")
    result = mod.load_inbox(path, "neoengine")
    assert result["ok"] is False
    assert "malformed" in result["error"].lower()


def test_atomic_readback_required(tmp_path: pathlib.Path, monkeypatch) -> None:
    path = tmp_path / "neoengine-controller-work-inbox.json"
    original = mod.json.loads
    calls = {"count": 0}
    def flaky(text: str):
        calls["count"] += 1
        raise json.JSONDecodeError("readback fail", text, 0)
    monkeypatch.setattr(mod.json, "loads", flaky)
    try:
        try:
            mod.atomic_write_json(path, {"schema": mod.SCHEMA, "org": "neoengine", "items": []})
        except RuntimeError as exc:
            assert "readback" in str(exc)
        else:
            raise AssertionError("atomic_write_json did not require readback parse")
    finally:
        monkeypatch.setattr(mod.json, "loads", original)


def test_org_scoped_inboxes_reject_cross_org(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "neowealth-controller-work-inbox.json"
    mod.atomic_write_json(path, {"schema": mod.SCHEMA, "org": "neoengine", "items": []})
    result = mod.load_inbox(path, "neowealth")
    assert result["ok"] is False
    assert "org mismatch" in result["error"]


def test_controller_consumes_queued_tier1_item_with_receipt(tmp_path: pathlib.Path, monkeypatch) -> None:
    path = tmp_path / "neoengine-controller-work-inbox.json"
    mod.upsert_work_item(path, org="neoengine", repo="neoengine-ai-org/neoengine", producer="hourly_watchdog", work_type="ci_pressure_recheck", priority="critical", state_fingerprint="fp", blocker="recheck", exact_instruction="write local receipt", authority_required="tier1_local")
    summary = mod.consume_inbox(path, "neoengine", max_items=5, branch_repair_enabled=False)
    data = json.loads(path.read_text())
    assert summary["done"] == 1
    assert data["items"][0]["status"] == "done"
    assert data["items"][0]["completion_receipt"]


def test_queued_item_not_silently_ignored_when_budget_exhausted(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "neoengine-controller-work-inbox.json"
    mod.upsert_work_item(path, org="neoengine", repo="neoengine-ai-org/neoengine", producer="hourly_watchdog", work_type="ci_pressure_recheck", priority="low", state_fingerprint="fp", blocker="x", exact_instruction="x", authority_required="tier1_local")
    summary = mod.consume_inbox(path, "neoengine", max_items=0)
    data = json.loads(path.read_text())
    assert summary["queued"] == 1
    assert data["items"][0]["blocker_receipt"]["reason"] == "budget exhausted before claim"


def test_existing_blocked_item_is_counted_in_summary(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "neoengine-controller-work-inbox.json"
    mod.upsert_work_item(path, org="neoengine", repo="neoengine-ai-org/neoengine", producer="hourly_watchdog", work_type="ci_pressure_recheck", priority="low", state_fingerprint="fp", blocker="x", exact_instruction="x", authority_required="tier1_local")
    data = json.loads(path.read_text())
    data["items"][0]["status"] = "blocked"
    data["items"][0]["blocker_receipt"] = {"outcome": "blocked", "blocker": "synthetic"}
    mod.atomic_write_json(path, data)
    summary = mod.consume_inbox(path, "neoengine", max_items=5)
    assert summary["blocked"] == 1
    assert summary["top_action"] == "none"


def test_unsafe_branch_namespace_collision_blocks_with_receipt(tmp_path: pathlib.Path, monkeypatch) -> None:
    path = tmp_path / "neoengine-controller-work-inbox.json"
    mod.upsert_work_item(path, org="neoengine", repo="neoengine-ai-org/neoengine", producer="hourly_watchdog", work_type="branch_namespace_collision", priority="high", state_fingerprint="fp", blocker="collision", exact_instruction="repair", authority_required="tier1_local")
    monkeypatch.setattr(mod, "evaluate_branch_namespace_collision", lambda item, workdir: {"safe": False, "blocker": "open PR uses colliding head", "checks": {"open_pr_head_collision": True}})
    summary = mod.consume_inbox(path, "neoengine", max_items=5, workdir=tmp_path, branch_repair_enabled=True)
    data = json.loads(path.read_text())
    assert summary["blocked"] == 1
    assert data["items"][0]["status"] == "blocked"
    assert data["items"][0]["blocker_receipt"]["classification"] == "BRANCH_NAMESPACE_COLLISION_REQUIRES_OPERATOR"


def safe_branch_collision_kwargs(archive_ref: str = "refs/heads/archive/controller-work-inbox/feature-test") -> dict[str, object]:
    return {
        "colliding_ref": "refs/heads/feature",
        "requested_ref": "refs/heads/feature/child",
        "collision_type": "parent_ref_blocks_nested_ref",
        "git_show_ref_evidence": ["refs/heads/feature verified by git show-ref"],
        "open_pr_uses_colliding_ref": "NO",
        "unpushed_commits_present": "NO",
        "canonical_checkout_dirty_or_detached": "YES",
        "safe_to_archive": "YES",
        "archive_candidate_ref": archive_ref,
        "exact_dispatcher_error": "cannot lock nested ref because parent ref exists",
        "evidence_source_paths": ["/tmp/synthetic-dispatcher.log"],
    }


def test_branch_namespace_collision_with_missing_colliding_ref_blocks(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "neoengine-controller-work-inbox.json"
    kwargs = safe_branch_collision_kwargs()
    kwargs.pop("colliding_ref")
    mod.upsert_work_item(path, org="neoengine", repo="neoengine-ai-org/neoengine", producer="hourly_watchdog", work_type="branch_namespace_collision", priority="high", state_fingerprint="missing-ref", blocker="collision", exact_instruction="repair", authority_required="tier1_local", **kwargs)
    summary = mod.consume_inbox(path, "neoengine", max_items=5, workdir=tmp_path, branch_repair_enabled=True)
    data = json.loads(path.read_text())
    assert summary["blocked"] == 1
    assert data["items"][0]["blocker_receipt"]["classification"] == "BRANCH_NAMESPACE_COLLISION_REQUIRES_OPERATOR"
    assert "UNKNOWN" in data["items"][0]["blocker_receipt"]["blocker"]


def test_branch_namespace_collision_with_open_pr_using_ref_blocks(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "neoengine-controller-work-inbox.json"
    kwargs = safe_branch_collision_kwargs()
    kwargs["open_pr_uses_colliding_ref"] = "YES"
    mod.upsert_work_item(path, org="neoengine", repo="neoengine-ai-org/neoengine", producer="hourly_watchdog", work_type="branch_namespace_collision", priority="high", state_fingerprint="open-pr", blocker="collision", exact_instruction="repair", authority_required="tier1_local", **kwargs)
    summary = mod.consume_inbox(path, "neoengine", max_items=5, workdir=tmp_path, branch_repair_enabled=True)
    data = json.loads(path.read_text())
    assert summary["blocked"] == 1
    assert "open PR" in data["items"][0]["blocker_receipt"]["blocker"]


def test_branch_namespace_collision_with_unpushed_commits_blocks(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "neoengine-controller-work-inbox.json"
    kwargs = safe_branch_collision_kwargs()
    kwargs["unpushed_commits_present"] = "YES"
    mod.upsert_work_item(path, org="neoengine", repo="neoengine-ai-org/neoengine", producer="hourly_watchdog", work_type="branch_namespace_collision", priority="high", state_fingerprint="unpushed", blocker="collision", exact_instruction="repair", authority_required="tier1_local", **kwargs)
    summary = mod.consume_inbox(path, "neoengine", max_items=5, workdir=tmp_path, branch_repair_enabled=True)
    data = json.loads(path.read_text())
    assert summary["blocked"] == 1
    assert "unpushed" in data["items"][0]["blocker_receipt"]["blocker"]


def test_branch_namespace_collision_never_repairs_from_vague_prose_fallback(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "f").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(repo), "branch", "feature"], check=True)
    path = tmp_path / "neoengine-controller-work-inbox.json"
    kwargs = safe_branch_collision_kwargs()
    kwargs["colliding_ref"] = "not-a-ref"
    mod.upsert_work_item(path, org="neoengine", repo="neoengine-ai-org/neoengine", producer="hourly_watchdog", work_type="branch_namespace_collision", priority="high", state_fingerprint="no-prose-fallback", blocker="cannot lock ref 'refs/heads/feature/child': 'refs/heads/feature' exists", exact_instruction="repair", authority_required="tier1_local", **kwargs)
    summary = mod.consume_inbox(path, "neoengine", max_items=5, workdir=repo, branch_repair_enabled=True)
    refs = subprocess.check_output(["git", "-C", str(repo), "for-each-ref", "--format=%(refname)", "refs/heads"], text=True)
    data = json.loads(path.read_text())
    assert summary["blocked"] == 1
    assert "refs/heads/feature\n" in refs
    assert data["items"][0]["blocker_receipt"]["classification"] == "BRANCH_NAMESPACE_COLLISION_REQUIRES_OPERATOR"
    assert "colliding_ref is not a local branch ref" in data["items"][0]["blocker_receipt"]["blocker"]


def test_safe_branch_namespace_collision_renames_local_ref(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "f").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(repo), "branch", "feature"], check=True)
    path = tmp_path / "neoengine-controller-work-inbox.json"
    mod.upsert_work_item(path, org="neoengine", repo="neoengine-ai-org/neoengine", producer="hourly_watchdog", work_type="branch_namespace_collision", priority="high", state_fingerprint="feature->feature/child", blocker="cannot lock ref 'refs/heads/feature/child': 'refs/heads/feature' exists", exact_instruction="repair", authority_required="tier1_local", **safe_branch_collision_kwargs())
    summary = mod.consume_inbox(path, "neoengine", max_items=5, workdir=repo, branch_repair_enabled=True)
    data = json.loads(path.read_text())
    refs = subprocess.check_output(["git", "-C", str(repo), "for-each-ref", "--format=%(refname)", "refs/heads"], text=True)
    assert summary["done"] == 1
    assert "refs/heads/archive/controller-work-inbox/" in refs
    assert "refs/heads/feature\n" not in refs
    assert data["items"][0]["completion_receipt"]["old_ref"] == "refs/heads/feature"


def test_no_hard_coded_live_examples() -> None:
    text = HELPER_PATH.read_text()
    forbidden = ["codex" + "-unwired-tests-auditor", "pr" + "517", "PR #" + "517", "#" + "517"]
    assert all(value not in text for value in forbidden)
