from __future__ import annotations

import json
from pathlib import Path

from neoengine_local.qwen35_lane_experience import (
    NON_CLAIMS,
    Qwen35LaneConfig,
    Qwen35LaneStatus,
    Qwen35Registry,
    build_bounded_prompt,
    classify_anti_loop,
    classify_qwen35_task,
    get_qwen35_task_recipe,
    main,
    render_lane_dashboard,
    render_operator_receipt,
    render_operator_summary,
    run_preflight_canary,
    score_qwen35_diff_risk,
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
    registry = Qwen35Registry(tmp_path / "registry.json")
    registry.upsert(
        repo="adingler711/neowealth",
        org="neowealth",
        model="qwen3.5:9b",
        qwen_cli_version="0.19.1",
        known_good_invocation=["--bare", "--sandbox", "-y"],
        known_bad_invocations=[["--bare", "--sandbox", "--approval-mode=yolo"]],
        canary_receipt_path=str(receipt),
        notes="test registry entry",
        verified_at="2026-06-26T00:00:00Z",
    )

    def runner(command: list[str], cwd: Path) -> tuple[int, str, str]:
        if command[:2] == ["git", "rev-parse"] and command[-1] == "--show-toplevel":
            return 0, str(worktree), ""
        if command[:3] == ["git", "config", "--get"]:
            return 0, "https://github.com/adingler711/neowealth.git", ""
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
        registry_path=registry.path,
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
    registry = Qwen35Registry(tmp_path / "registry.json")
    registry.upsert(
        repo="neoengine-ai-org/hermes-agent",
        org="neoengine-ai-org",
        model="qwen3.5:9b",
        qwen_cli_version="0.19.1",
        known_good_invocation=["--bare", "--sandbox", "--approval-mode=yolo"],
        known_bad_invocations=[["--bare", "--sandbox", "-y"]],
        canary_receipt_path=str(receipt),
        notes="test registry entry",
        verified_at="2026-06-26T00:00:00Z",
    )

    def runner(command: list[str], cwd: Path) -> tuple[int, str, str]:
        if command[:2] == ["git", "rev-parse"] and command[-1] == "--show-toplevel":
            return 0, str(worktree), ""
        if command[:3] == ["git", "config", "--get"]:
            return 0, "git@github.com:neoengine-ai-org/hermes-agent.git", ""
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
        registry_path=registry.path,
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



def test_preflight_registry_enforcement_refuses_wrong_repo_invocation(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    receipt = tmp_path / "canary.json"
    registry = Qwen35Registry(tmp_path / "registry.json")
    registry.upsert(
        repo="adingler711/neowealth",
        org="neowealth",
        model="qwen3.5:9b",
        qwen_cli_version="0.19.1",
        known_good_invocation=["--bare", "--sandbox", "-y"],
        known_bad_invocations=[["--bare", "--sandbox", "--approval-mode=yolo"]],
        canary_receipt_path="/receipts/nw.json",
        notes="NeoWealth is intentionally different from NeoEngine.",
        verified_at="2026-06-26T00:00:00Z",
    )

    def runner(command: list[str], cwd: Path) -> tuple[int, str, str]:
        raise AssertionError(f"registry refusal should happen before tool execution: {command}")

    result = run_preflight_canary(
        repo="adingler711/neowealth",
        org="neowealth",
        worktree=worktree,
        model="qwen3.5:9b",
        invocation=["--bare", "--sandbox", "--approval-mode=yolo"],
        receipt_path=receipt,
        registry_path=registry.path,
        enforce_registry=True,
        runner=runner,
        now="2026-06-26T00:00:00Z",
    )

    assert result["status"] == "FAILED_TOOLING_OR_CONTEXT"
    assert result["failure_mode"] == "INVOCATION_NOT_ALLOWED_FOR_REPO"
    assert result["registry_enforced"] is True


def test_post_run_git_command_failure_blocks_no_change_positive_verdict(tmp_path: Path) -> None:
    receipt = tmp_path / "completion.json"
    receipt.write_text(json.dumps({"terminal_status": "NO_CHANGE_WITH_EVIDENCE"}))

    def runner(command: list[str], cwd: Path) -> tuple[int, str, str]:
        if command[:2] == ["git", "status"]:
            return 1, "", "not a git repository"
        if command[:2] == ["git", "diff"]:
            return 0, "", ""
        if command[:2] == ["git", "rev-parse"]:
            return 0, "abc123", ""
        raise AssertionError(f"unexpected command: {command}")

    result = verify_post_run(repo="repo", worktree=tmp_path, completion_receipt=receipt, runner=runner)

    assert result["verdict"] == "VERIFIED_TOOLING_OR_CONTEXT_FAILURE"
    assert any("git status failed" in blocker for blocker in result["blockers"])


def test_post_run_git_command_failure_blocks_productive_positive_verdict(tmp_path: Path) -> None:
    receipt = tmp_path / "completion.json"
    receipt.write_text(json.dumps({"terminal_status": "PRODUCTIVE_DIFF_WITH_EVIDENCE", "commit_sha": "abc123"}))

    def runner(command: list[str], cwd: Path) -> tuple[int, str, str]:
        if command[:2] == ["git", "status"]:
            return 0, "", ""
        if command[:2] == ["git", "diff"]:
            return 1, "", "diff failed"
        if command[:2] == ["git", "rev-parse"]:
            return 0, "abc123", ""
        raise AssertionError(f"unexpected command before fail-closed return: {command}")

    result = verify_post_run(repo="repo", worktree=tmp_path, completion_receipt=receipt, runner=runner)

    assert result["verdict"] == "VERIFIED_TOOLING_OR_CONTEXT_FAILURE"
    assert any("git diff failed" in blocker for blocker in result["blockers"])


def test_post_run_rejects_fabricated_commit_sha(tmp_path: Path) -> None:
    receipt = tmp_path / "completion.json"
    receipt.write_text(json.dumps({"terminal_status": "PRODUCTIVE_DIFF_WITH_EVIDENCE", "commit_sha": "deadbeef"}))

    def runner(command: list[str], cwd: Path) -> tuple[int, str, str]:
        if command[:2] == ["git", "status"]:
            return 0, "", ""
        if command[:2] == ["git", "diff"]:
            return 0, "", ""
        if command[:2] == ["git", "rev-parse"] and command[-1] == "HEAD":
            return 0, "abc123", ""
        if command[:2] == ["git", "cat-file"]:
            return 1, "", "missing"
        if command[:2] == ["git", "rev-parse"]:
            return 1, "", "unknown revision"
        if command[:2] == ["git", "show"]:
            return 1, "", "bad object"
        raise AssertionError(f"unexpected command: {command}")

    result = verify_post_run(repo="repo", worktree=tmp_path, completion_receipt=receipt, runner=runner)

    assert result["verdict"] == "CLAIMS_EXCEED_EVIDENCE"
    assert "commit_sha claimed but commit does not exist" in result["blockers"]


def test_post_run_claimed_commit_must_match_observed_head(tmp_path: Path) -> None:
    receipt = tmp_path / "completion.json"
    receipt.write_text(json.dumps({"terminal_status": "PRODUCTIVE_DIFF_WITH_EVIDENCE", "commit_sha": "abc123"}))

    def runner(command: list[str], cwd: Path) -> tuple[int, str, str]:
        if command[:2] == ["git", "status"]:
            return 0, "", ""
        if command[:2] == ["git", "diff"]:
            return 0, "", ""
        if command[:2] == ["git", "rev-parse"] and command[-1] == "HEAD":
            return 0, "observed", ""
        if command[:2] == ["git", "cat-file"]:
            return 0, "", ""
        if command[:2] == ["git", "rev-parse"]:
            return 0, "different", ""
        if command[:2] == ["git", "show"]:
            return 0, "different file.py", ""
        raise AssertionError(f"unexpected command: {command}")

    result = verify_post_run(repo="repo", worktree=tmp_path, completion_receipt=receipt, runner=runner)

    assert result["verdict"] == "CLAIMS_EXCEED_EVIDENCE"
    assert "commit_sha claimed but does not match observed HEAD" in result["blockers"]


def test_cli_verify_exits_nonzero_for_blocking_verdict(tmp_path: Path, capsys) -> None:
    receipt = tmp_path / "completion.json"
    receipt.write_text(json.dumps({"terminal_status": "INVALID_STATUS"}))

    exit_code = main(["verify", "--repo", "repo", "--worktree", str(tmp_path), "--completion-receipt", str(receipt)])

    assert exit_code == 2
    assert "INVALID_TERMINAL_RETURN" in capsys.readouterr().out


def test_render_operator_summary_shape_and_non_claim_boundary() -> None:
    summary = render_operator_summary(
        what_happened="Qwen35 candidate lane was inspected.",
        what_changed="No production state changed.",
        what_did_not_happen="; ".join(NON_CLAIMS),
        evidence_class="candidate output evidence only",
        required_next_action="independent review required",
        safe_next_prompt="Return PRODUCTIVE_DIFF_WITH_EVIDENCE or NO_CHANGE_WITH_EVIDENCE only.",
    )

    for heading in [
        "## What happened",
        "## What changed",
        "## What did not happen",
        "## Evidence class",
        "## Required next human/frontier action",
        "## Safe next prompt, if recycle is needed",
    ]:
        assert heading in summary
    for non_claim in [
        "not deployed",
        "not live",
        "not accepted",
        "not merged unless independently verified",
        "not merge evidence unless PR/CI/merge state are independently verified",
        "no branch-protection mutation",
        "no production cron",
        "no external notification delivery without separate authorization",
        "QWEN35 remains candidate-only",
    ]:
        assert non_claim in NON_CLAIMS
        assert non_claim in summary



def test_post_run_rejects_matching_empty_commit_without_changed_files(tmp_path: Path) -> None:
    receipt = tmp_path / "completion.json"
    receipt.write_text(json.dumps({"terminal_status": "PRODUCTIVE_DIFF_WITH_EVIDENCE", "commit_sha": "abc123"}))

    def runner(command: list[str], cwd: Path) -> tuple[int, str, str]:
        if command[:2] == ["git", "status"]:
            return 0, "", ""
        if command[:2] == ["git", "diff"] and "diff-tree" not in command:
            return 0, "", ""
        if command[:2] == ["git", "rev-parse"] and command[-1] == "HEAD":
            return 0, "abc123", ""
        if command[:2] == ["git", "cat-file"]:
            return 0, "", ""
        if command[:2] == ["git", "rev-parse"]:
            return 0, "abc123", ""
        if command[:2] == ["git", "show"]:
            return 0, "abc123 empty commit message", ""
        if command[:2] == ["git", "diff-tree"]:
            return 0, "", ""
        raise AssertionError(f"unexpected command: {command}")

    result = verify_post_run(repo="repo", worktree=tmp_path, completion_receipt=receipt, runner=runner)

    assert result["verdict"] == "CLAIMS_EXCEED_EVIDENCE"
    assert "commit_sha claimed but commit has no changed files" in result["blockers"]


def test_post_run_preserved_negative_non_claims_do_not_trigger_overclaim_blocker(tmp_path: Path) -> None:
    receipt = tmp_path / "completion.json"
    receipt.write_text(json.dumps({"terminal_status": "NO_CHANGE_WITH_EVIDENCE", "non_claims_preserved": NON_CLAIMS}))

    def runner(command: list[str], cwd: Path) -> tuple[int, str, str]:
        if command[:2] == ["git", "status"]:
            return 0, "", ""
        if command[:2] == ["git", "diff"]:
            return 0, "", ""
        if command[:2] == ["git", "rev-parse"]:
            return 0, "abc123", ""
        raise AssertionError(f"unexpected command: {command}")

    result = verify_post_run(repo="repo", worktree=tmp_path, completion_receipt=receipt, runner=runner)

    assert result["verdict"] == "VERIFIED_NO_CHANGE_WITH_EVIDENCE"
    assert result["blockers"] == []



def test_preflight_requires_registry_by_default(tmp_path: Path) -> None:
    result = run_preflight_canary(
        repo="neoengine-ai-org/hermes-agent",
        org="neoengine-ai-org",
        worktree=tmp_path,
        model="qwen3.5:9b",
        invocation=["--bare", "--sandbox", "--approval-mode=yolo"],
        receipt_path=tmp_path / "canary.json",
        runner=lambda command, cwd: (_ for _ in ()).throw(AssertionError(f"registry failure should precede {command}")),
    )

    assert result["status"] == "FAILED_TOOLING_OR_CONTEXT"
    assert result["failure_mode"] == "INVOCATION_REGISTRY_REQUIRED"
    assert result["registry_enforced"] is True



def test_preflight_registry_refuses_declared_repo_remote_mismatch(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    receipt = tmp_path / "canary.json"
    registry = Qwen35Registry(tmp_path / "registry.json")
    registry.upsert(
        repo="neoengine-ai-org/hermes-agent",
        org="neoengine-ai-org",
        model="qwen3.5:9b",
        qwen_cli_version="0.19.1",
        known_good_invocation=["--bare", "--sandbox", "--approval-mode=yolo"],
        known_bad_invocations=[["--bare", "--sandbox", "-y"]],
        canary_receipt_path=str(receipt),
        notes="test registry entry",
        verified_at="2026-06-26T00:00:00Z",
    )

    def runner(command: list[str], cwd: Path) -> tuple[int, str, str]:
        if command[:2] == ["git", "rev-parse"] and command[-1] == "--show-toplevel":
            return 0, str(worktree), ""
        if command[:3] == ["git", "config", "--get"]:
            return 0, "https://github.com/adingler711/neowealth.git", ""
        raise AssertionError(f"remote mismatch should refuse before further tool execution: {command}")

    result = run_preflight_canary(
        repo="neoengine-ai-org/hermes-agent",
        org="neoengine-ai-org",
        worktree=worktree,
        model="qwen3.5:9b",
        invocation=["--bare", "--sandbox", "--approval-mode=yolo"],
        receipt_path=receipt,
        registry_path=registry.path,
        runner=runner,
        now="2026-06-26T00:00:00Z",
    )

    assert result["status"] == "FAILED_TOOLING_OR_CONTEXT"
    assert result["failure_mode"] == "REPO_REMOTE_MISMATCH"


def test_post_run_non_claim_prefix_cannot_hide_overclaim(tmp_path: Path) -> None:
    receipt = tmp_path / "completion.json"
    receipt.write_text(json.dumps({"terminal_status": "NO_CHANGE_WITH_EVIDENCE", "note": "not deployed; live and accepted"}))

    def runner(command: list[str], cwd: Path) -> tuple[int, str, str]:
        if command[:2] == ["git", "status"]:
            return 0, "", ""
        if command[:2] == ["git", "diff"]:
            return 0, "", ""
        if command[:2] == ["git", "rev-parse"]:
            return 0, "abc123", ""
        raise AssertionError(f"unexpected command: {command}")

    result = verify_post_run(repo="repo", worktree=tmp_path, completion_receipt=receipt, runner=runner)

    assert result["verdict"] == "CLAIMS_EXCEED_EVIDENCE"
    assert "completion text may exceed non-claim ceiling" in result["blockers"]


def test_post_run_productive_diff_requires_claimed_files_match_observed_diff(tmp_path: Path) -> None:
    receipt = tmp_path / "completion.json"
    receipt.write_text(json.dumps({"terminal_status": "PRODUCTIVE_DIFF_WITH_EVIDENCE", "changed_files": ["wrong.py"]}))

    def runner(command: list[str], cwd: Path) -> tuple[int, str, str]:
        if command[:2] == ["git", "status"]:
            return 0, " M actual.py", ""
        if command[:2] == ["git", "diff"] and "--name-only" in command:
            return 0, "actual.py\n", ""
        if command[:2] == ["git", "diff"]:
            return 0, " actual.py | 1 +", ""
        if command[:2] == ["git", "rev-parse"]:
            return 0, "abc123", ""
        raise AssertionError(f"unexpected command: {command}")

    result = verify_post_run(repo="repo", worktree=tmp_path, completion_receipt=receipt, runner=runner)

    assert result["verdict"] == "CLAIMS_EXCEED_EVIDENCE"
    assert "dirty worktree status blocks positive verifier outcome" in result["blockers"]


def test_post_run_productive_diff_rejects_uncommitted_diff_even_when_claimed_files_match(tmp_path: Path) -> None:
    receipt = tmp_path / "completion.json"
    receipt.write_text(json.dumps({"terminal_status": "PRODUCTIVE_DIFF_WITH_EVIDENCE", "changed_files": ["actual.py"]}))

    def runner(command: list[str], cwd: Path) -> tuple[int, str, str]:
        if command[:2] == ["git", "status"]:
            return 0, " M actual.py", ""
        if command[:2] == ["git", "diff"] and "--name-only" in command:
            return 0, "actual.py\n", ""
        if command[:2] == ["git", "diff"]:
            return 0, " actual.py | 1 +", ""
        if command[:2] == ["git", "rev-parse"]:
            return 0, "abc123", ""
        raise AssertionError(f"unexpected command: {command}")

    result = verify_post_run(repo="repo", worktree=tmp_path, completion_receipt=receipt, runner=runner)

    assert result["verdict"] == "CLAIMS_EXCEED_EVIDENCE"
    assert "dirty worktree status blocks positive verifier outcome" in result["blockers"]
    assert result["git_diff_files"] == ["actual.py"]



def test_post_run_productive_commit_proof_rejects_dirty_untracked_worktree(tmp_path: Path) -> None:
    receipt = tmp_path / "completion.json"
    receipt.write_text(json.dumps({"terminal_status": "PRODUCTIVE_DIFF_WITH_EVIDENCE", "commit_sha": "abc123"}))

    def runner(command: list[str], cwd: Path) -> tuple[int, str, str]:
        if command[:2] == ["git", "status"]:
            return 0, "?? stray.txt", ""
        if command[:2] == ["git", "diff"]:
            return 0, "", ""
        if command[:2] == ["git", "rev-parse"]:
            return 0, "abc123", ""
        if command[:2] == ["git", "cat-file"]:
            return 0, "", ""
        if command[:2] == ["git", "show"]:
            return 0, "abc123 message\nactual.py", ""
        if command[:2] == ["git", "diff-tree"]:
            return 0, "actual.py\n", ""
        raise AssertionError(f"unexpected command: {command}")

    result = verify_post_run(repo="repo", worktree=tmp_path, completion_receipt=receipt, runner=runner)

    assert result["verdict"] == "CLAIMS_EXCEED_EVIDENCE"
    assert "dirty worktree status blocks positive verifier outcome" in result["blockers"]


def test_work_picker_allows_low_risk_docs_and_blocks_protected_surfaces() -> None:
    safe = classify_qwen35_task("Refresh docs/runbook receipts for stale Qwen35 adoption notes")
    forbidden = classify_qwen35_task("Deploy production cron and update branch protection for billing auth")
    broad = classify_qwen35_task("Refactor the whole runtime service architecture")

    assert safe["classification"] == "QWEN35_SAFE_LOW_RISK"
    assert safe["allowed"] is True
    assert "docs/runbook maintenance" in safe["matched_safe_classes"]
    assert forbidden["classification"] == "QWEN35_FORBIDDEN_PROTECTED_SURFACE"
    assert forbidden["allowed"] is False
    assert "deployments" in forbidden["matched_red_lines"]
    assert "financial/security-sensitive logic" in forbidden["matched_red_lines"]
    assert broad["classification"] == "QWEN35_BAD_FIT_TOO_BROAD"


def test_task_menu_recipes_define_bounded_commands_status_and_change_patterns() -> None:
    recipe = get_qwen35_task_recipe("receipt_nonclaim_audit")

    assert recipe["task_type"] == "receipt_nonclaim_audit"
    assert recipe["max_wall_time"] == "10m"
    assert recipe["max_tool_calls"] <= 20
    assert recipe["expected_terminal_statuses"] == [
        "NO_CHANGE_WITH_EVIDENCE",
        "PRODUCTIVE_DIFF_WITH_EVIDENCE",
        "FAILED_TOOLING_OR_CONTEXT",
    ]
    assert "git status --porcelain" in recipe["allowed_commands"]
    assert "git push" in recipe["forbidden_commands"]
    assert recipe["allowed_changed_file_globs"] == ["docs/**", "*.md", "**/*.md"]
    assert "verifier_rule" in recipe


def test_operator_receipt_normalizes_verifier_output_and_preserves_non_claims() -> None:
    receipt = render_operator_receipt(
        repo="neoengine-ai-org/hermes-agent",
        branch="qwen35/receipt-audit",
        head_sha="abc123",
        task_type="receipt_nonclaim_audit",
        terminal_status="NO_CHANGE_WITH_EVIDENCE",
        changed_files=[],
        commands_observed=[["git", "status", "--porcelain"], ["git", "rev-parse", "HEAD"]],
        verifier_result="VERIFIED_NO_CHANGE_WITH_EVIDENCE",
        operator_action_needed="None; preserve as no-op candidate evidence.",
    )

    assert receipt.startswith("QWEN35_CANDIDATE_ASSISTANT_RECEIPT")
    assert "repo: neoengine-ai-org/hermes-agent" in receipt
    assert "changed_files: none" in receipt
    assert "commands_observed:" in receipt
    assert "- git status --porcelain" in receipt
    assert "non_claims:" in receipt
    assert "not merged" in receipt
    assert "QWEN35 remains candidate-only" in receipt


def test_diff_risk_scoring_classifies_safe_docs_tests_tooling_and_forbidden_paths() -> None:
    assert score_qwen35_diff_risk([])["risk"] == "RISK_0_NO_CHANGE"
    assert score_qwen35_diff_risk(["docs/qwen35.md"])["risk"] == "RISK_1_DOCS_ONLY"
    assert score_qwen35_diff_risk(["tests/neoengine_local/test_qwen35_lane_experience.py"])["risk"] == "RISK_2_TEST_OR_FIXTURE_ONLY"
    assert score_qwen35_diff_risk(["neoengine_local/qwen35_lane_experience.py"])["risk"] == "RISK_3_LOCAL_TOOLING_ONLY"
    forbidden = score_qwen35_diff_risk([".github/workflows/deploy-site.yml", "src/auth/session.py"])
    assert forbidden["risk"] == "RISK_5_FORBIDDEN_PROTECTED_SURFACE"
    assert "deployment/production workflow" in forbidden["matched_forbidden_surfaces"]
    assert "auth/security path" in forbidden["matched_forbidden_surfaces"]


def test_cli_exposes_work_picker_recipe_and_diff_risk(capsys) -> None:
    assert main(["pick-task", "--description", "Refresh docs runbook receipts"]) == 0
    picked = json.loads(capsys.readouterr().out)
    assert picked["classification"] == "QWEN35_SAFE_LOW_RISK"

    assert main(["recipe", "--task-type", "receipt_nonclaim_audit"]) == 0
    recipe = json.loads(capsys.readouterr().out)
    assert recipe["task_type"] == "receipt_nonclaim_audit"

    assert main(["risk", "--changed-file", ".github/workflows/deploy-site.yml"]) == 2
    risk = json.loads(capsys.readouterr().out)
    assert risk["risk"] == "RISK_5_FORBIDDEN_PROTECTED_SURFACE"
