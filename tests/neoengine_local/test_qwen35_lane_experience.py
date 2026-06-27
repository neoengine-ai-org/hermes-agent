from __future__ import annotations

import json
from pathlib import Path

from neoengine_local.qwen35_lane_experience import (
    Qwen35LaneConfig,
    Qwen35LaneStatus,
    Qwen35Registry,
    build_bounded_prompt,
    classify_anti_loop,
    render_lane_dashboard,
    run_preflight_canary,
    verify_post_run,
)


def test_registry_blocks_neowealth_from_neoengine_invocation_without_fresh_canary(tmp_path: Path) -> None:
    registry_path = tmp_path / "invocations.json"
    registry = Qwen35Registry(registry_path)
    registry.upsert(
        repo="neoengine-ai-org/hermes-agent",
        org="neoengine",
        model="qwen3.5:9b",
        qwen_cli_version="0.19.1",
        known_good_invocation=["--bare", "--sandbox", "--approval-mode=yolo"],
        known_bad_invocations=[["--bare", "--sandbox", "-y"]],
        canary_receipt_path="/receipts/ne.json",
        notes="NeoEngine accepts approval-mode=yolo.",
        verified_at="2026-06-26T00:00:00Z",
    )
    registry.upsert(
        repo="adingler711/neowealth",
        org="neowealth",
        model="qwen3.5:9b",
        qwen_cli_version="0.19.1",
        known_good_invocation=["--bare", "--sandbox", "-y"],
        known_bad_invocations=[["--bare", "--sandbox", "--approval-mode=yolo"]],
        canary_receipt_path="/receipts/nw.json",
        notes="NeoWealth CLI requires -y in noninteractive mode.",
        verified_at="2026-06-26T00:00:00Z",
    )

    assert registry.allowed_invocation("adingler711/neowealth", ["--bare", "--sandbox", "-y"])
    assert not registry.allowed_invocation(
        "adingler711/neowealth", ["--bare", "--sandbox", "--approval-mode=yolo"]
    )


def test_preflight_refuses_dirty_worktree_before_real_launch(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    receipt = tmp_path / "canary.json"

    def runner(command: list[str], cwd: Path) -> tuple[int, str, str]:
        if command[:2] == ["git", "rev-parse"] and command[-1] == "--show-toplevel":
            return 0, str(worktree), ""
        if command[:2] == ["git", "branch"]:
            return 0, "qwen35/networth-render-island-audit", ""
        if command[:2] == ["git", "rev-parse"] and command[-1] == "HEAD":
            return 0, "abc123", ""
        if command[:2] == ["git", "status"]:
            return 0, " M src/app.ts", ""
        raise AssertionError(f"unexpected command after dirty refusal: {command}")

    result = run_preflight_canary(
        repo="adingler711/neowealth",
        org="neowealth",
        worktree=worktree,
        model="qwen3.5:9b",
        invocation=["--bare", "--sandbox", "-y"],
        receipt_path=receipt,
        runner=runner,
        now="2026-06-26T00:00:00Z",
    )

    assert result["status"] == "FAILED_TOOLING_OR_CONTEXT"
    assert result["failure_mode"] == "DIRTY_WORKTREE_REFUSED"
    assert json.loads(receipt.read_text())["failure_mode"] == "DIRTY_WORKTREE_REFUSED"


def test_preflight_records_successful_tool_execution_receipt(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    (worktree / "README.md").write_text("ok")
    receipt = tmp_path / "canary.json"

    def runner(command: list[str], cwd: Path) -> tuple[int, str, str]:
        if command[:2] == ["git", "rev-parse"] and command[-1] == "--show-toplevel":
            return 0, str(worktree), ""
        if command[:2] == ["git", "branch"]:
            return 0, "main", ""
        if command[:2] == ["git", "rev-parse"] and command[-1] == "HEAD":
            return 0, "abc123", ""
        if command[:2] == ["git", "status"]:
            return 0, "", ""
        if command[0] == "qwen" and "--version" in command:
            return 0, "0.19.1", ""
        if command[:2] == ["curl", "-fsS"]:
            return 0, '{"model":"qwen3.5:9b"}', ""
        if command[0] == "find":
            return 0, "README.md", ""
        raise AssertionError(f"unexpected command: {command}")

    result = run_preflight_canary(
        repo="neoengine-ai-org/hermes-agent",
        org="neoengine",
        worktree=worktree,
        model="qwen3.5:9b",
        invocation=["--bare", "--sandbox", "--approval-mode=yolo"],
        receipt_path=receipt,
        runner=runner,
        now="2026-06-26T00:00:00Z",
    )

    assert result["status"] == "PASS"
    assert result["native_file_tools_execute"] is True
    assert result["minimal_inspection"] == ["README.md"]
    saved = json.loads(receipt.read_text())
    assert saved["invocation"] == ["--bare", "--sandbox", "--approval-mode=yolo"]


def test_dashboard_separates_evidence_classes(tmp_path: Path) -> None:
    status = Qwen35LaneStatus(
        repo="adingler711/neowealth",
        worktree="/wt",
        branch="qwen35/networth-render-island-audit",
        pgid="650",
        session="proc_x",
        invocation="--bare --sandbox -y",
        started_at="2026-06-26T00:00:00Z",
        elapsed="5m",
        current_classification="candidate active",
        current_evidence_class="controlled execution evidence",
        latest_log_path="/logs/nw.log",
        latest_receipt_path="/receipts/nw.json",
        completion_receipt_exists=False,
        repo_diff_exists=False,
        commit_exists=False,
        pr_exists=False,
        tests_claimed=False,
        claimed_tests_independently_verified=False,
    )

    markdown = render_lane_dashboard([status], generated_at="2026-06-26T00:05:00Z")

    assert "controlled execution evidence" in markdown
    assert "candidate output evidence" in markdown
    assert "delivery evidence" in markdown
    assert "merge evidence" in markdown
    assert "acceptance evidence" in markdown
    assert "completion receipt: no" in markdown


def test_anti_loop_classifies_tooling_failures_and_vague_success() -> None:
    assert classify_anti_loop("requires user approval\nrequires user approval") == "FAILED_TOOLING_OR_CONTEXT"
    assert classify_anti_loop("success! done. no changes needed") == "FAILED_TOOLING_OR_CONTEXT"
    assert classify_anti_loop("PRODUCTIVE_DIFF_WITH_EVIDENCE\nchanged_files: ['a.py']") is None


def test_prompt_template_is_bounded_and_preserves_red_lines() -> None:
    config = Qwen35LaneConfig(
        repo="adingler711/neowealth",
        org="neowealth",
        worktree="/wt",
        branch="qwen35/networth-render-island-audit",
        model="qwen3.5:9b",
        invocation=["--bare", "--sandbox", "-y"],
        objective="Audit render island after #650.\nPick smallest gap.",
    )
    prompt = build_bounded_prompt(config)

    assert "max files to inspect before choosing a target: 12" in prompt
    assert "max files to change: 3" in prompt
    assert "no broad refactor permission" in prompt
    assert "no duplicate work if the target already landed on main" in prompt
    assert "no merge" in prompt


def test_post_run_verifier_rejects_claims_that_exceed_artifacts(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    receipt = tmp_path / "completion.json"
    receipt.write_text(json.dumps({"terminal_status": "PRODUCTIVE_DIFF_WITH_EVIDENCE", "pr_url": "https://example/pr/1"}))

    def runner(command: list[str], cwd: Path) -> tuple[int, str, str]:
        if command[:2] == ["git", "status"]:
            return 0, "", ""
        if command[:2] == ["git", "diff"]:
            return 0, "", ""
        if command[:2] == ["git", "rev-parse"]:
            return 0, "abc123", ""
        if command[0] == "gh":
            return 1, "", "not found"
        raise AssertionError(f"unexpected command: {command}")

    result = verify_post_run(
        repo="adingler711/neowealth",
        worktree=worktree,
        completion_receipt=receipt,
        runner=runner,
        now="2026-06-26T00:00:00Z",
    )

    assert result["verdict"] == "CLAIMS_EXCEED_EVIDENCE"
    assert "pr_url claimed but PR was not independently verified" in result["blockers"]


def test_post_run_verifier_accepts_no_change_with_clean_evidence(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    receipt = tmp_path / "completion.json"
    receipt.write_text(json.dumps({"terminal_status": "NO_CHANGE_WITH_EVIDENCE"}))

    def runner(command: list[str], cwd: Path) -> tuple[int, str, str]:
        if command[:2] == ["git", "status"]:
            return 0, "", ""
        if command[:2] == ["git", "diff"]:
            return 0, "", ""
        if command[:2] == ["git", "rev-parse"]:
            return 0, "abc123", ""
        raise AssertionError(f"unexpected command: {command}")

    result = verify_post_run(
        repo="neoengine-ai-org/hermes-agent",
        worktree=worktree,
        completion_receipt=receipt,
        runner=runner,
        now="2026-06-26T00:00:00Z",
    )

    assert result["verdict"] == "VERIFIED_NO_CHANGE_WITH_EVIDENCE"
