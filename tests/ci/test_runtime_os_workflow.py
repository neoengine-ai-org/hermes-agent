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
    assert 'RUNTIME_OS_POLICY_VERSION: "2.1.0"' in text


def test_runtime_os_workflow_preserves_pins_and_per_file_isolation() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "astral-sh/setup-uv@fac544c07dec837d0ccb6301d7b5580bf5edae39" in text
    assert "RG_SHA256=1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599" in text
    assert 'scripts/run_tests.sh --files "$SELECTED_FILES"' in text
    assert "SELECTED_FILES: ${{ matrix.files }}" in text
    assert "--files '${{ matrix.files }}'" not in text
    assert "persist-credentials: false" in text


def test_runtime_os_workflow_is_advisory_and_has_no_write_permission() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "contents: read" in text
    assert "pull-requests: write" not in text
    assert "does not merge, label, review, or alter branch protection" in text
    assert "trusted/scripts/review_receipt_validator.py" in text
