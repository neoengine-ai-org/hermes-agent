from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ci-runtime-os-advisory.yml"


def test_runtime_os_workflow_has_stable_advisory_contexts_and_qwen_runner() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    for context in ("Hermes CI required", "Review evidence required", "Merge admission"):
        assert f"name: {context}" in text
    assert "[self-hosted, Linux, x64, neoengine-shared-linux, qwen-ops]" in text
    assert "branches: [main]" in text
    assert "pull_request_target:" in text
    assert "merge_group:" in text
    assert "edited, labeled, unlabeled" in text
    assert "pull_request_review:" in text
    assert "converted_to_draft" in text
    assert "github.event.merge_group.head_sha" in text
    assert (
        "Merge-group review/admission is fail-closed until protected authority "
        "supplies complete constituent membership and per-member risk classification"
    ) in text
    assert 'RUNTIME_OS_POLICY_VERSION: "2.1.0"' in text


def test_runtime_os_workflow_preserves_pins_and_per_file_isolation() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "astral-sh/setup-uv@fac544c07dec837d0ccb6301d7b5580bf5edae39" in text
    assert 'scripts/run_tests.sh -j 4 --files "$SELECTED_FILES"' in text
    assert "SELECTED_FILES: ${{ matrix.files }}" in text
    assert "--files '${{ matrix.files }}'" not in text
    assert "persist-credentials: false" in text
    assert text.count("uv sync --locked --python 3.11 --extra all --extra dev") == 1
    assert text.count("astral-sh/setup-uv@fac544c07dec837d0ccb6301d7b5580bf5edae39") == 1
    assert text.count("RG_SHA256=1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599") == 1
    assert "hermes-ci-fast-${{ needs.preflight.outputs.environment_digest }}" in text
    assert "needs: [preflight, environment, test, e2e]" in text
    assert 'test "$ENVIRONMENT" = success -o "$ENVIRONMENT" = skipped' in text
    assert "one infra-only retry" in text
    assert "runtime-os-duration-${test_manifest_digest}-${dependency_digest}" in text
    assert "Publish protected-main duration telemetry" in text
    assert "actions/cache/save@27d5ce7f107fe9357f9df03efb73ab90386fccae" in text


def test_runtime_os_workflow_is_advisory_and_has_no_write_permission() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "contents: read" in text
    assert "pull-requests: write" not in text
    assert "does not merge, label, review, or alter branch protection" in text
    assert "trusted/scripts/review_receipt_validator.py" in text
    assert "github.rest.pulls.listReviews" in text
    assert "review.commit_id !== process.env.EXPECTED_HEAD" in text
    assert "builders.has(login)" in text
    assert "OWNER', 'MEMBER', 'COLLABORATOR" in text
    assert "latestByReviewer" in text
    assert "receiptHeadings.length !== 1" in text
    assert r"accepted.join('\\n')" not in text
    assert r"accepted.join('\n')" in text
    assert "protected specialist review transport is not authenticated" in text
    assert "RECEIPT_TTL_HOURS" in text
    assert "--pr-body authenticated-reviews.md" in text
    assert "! printf '%s' \"$LABELS\"" not in text
    assert "Merge admission denied by an exact-head opt-out label." in text
    assert "base.commit.sha !== process.env.EXPECTED_BASE" in text
    assert "pull.mergeable !== true" in text
    assert "manual-merge" in text
    assert "no-auto-merge" in text
    assert "String(label.name).toLowerCase()" in text
    assert "changesRequested" in text
    assert "reviewState.reviewDecision === 'CHANGES_REQUESTED'" in text
