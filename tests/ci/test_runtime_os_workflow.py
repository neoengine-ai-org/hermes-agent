from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ci-runtime-os-advisory.yml"
CANDIDATE = ROOT / ".github/workflows/ci-runtime-os-candidate.yml"
PR_EVENTS = "fromJSON('[\"pull_request_target\",\"pull_request_review\"]')"


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
    assert "needs: [preflight, environment, test, e2e, candidate-proof]" in text
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


def _jobs(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]


def test_privileged_workflow_never_executes_pull_request_head() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    jobs = _jobs(WORKFLOW)
    for job_id in ("environment", "test", "e2e"):
        assert f"!contains({PR_EVENTS}, github.event_name)" in jobs[job_id]["if"]
        for step in jobs[job_id]["steps"]:
            if str(step.get("uses", "")).startswith("actions/checkout"):
                assert step["with"]["ref"] == "${{ github.event.merge_group.head_sha || github.sha }}"
    assert "Checkout same-repository candidate" not in text
    assert 'git -C .runtime-os-trusted fetch --no-tags --no-recurse-submodules origin "$HEAD_SHA"' in text
    assert "--no-ext-diff --no-textconv --name-only" in text
    waiter = jobs["candidate-proof"]
    assert waiter["name"] == "Await unprivileged candidate proof"
    assert waiter["permissions"] == {"actions": "read"}
    assert waiter["runs-on"] == "ubuntu-latest"
    script = waiter["steps"][0]["with"]["script"]
    assert "run.head_sha === head" in script
    assert "run.head_repository.full_name === repository" in script
    assert "latest.conclusion === 'success'" in script
    assert "(run.pull_requests || []).length === 1" in script
    assert "run.pull_requests[0].number === prNumber" in script
    assert "latest.conclusion === 'cancelled' && now <= discoveryDeadline" in script
    assert "process.env.DEFINITION_CHANGED !== 'false'" in script
    assert "run.display_title === expectedTitle" in script
    assert "Runtime OS candidate ${head} on ${process.env.EXPECTED_BASE}" in script
    assert "grep -qxF .github/workflows/ci-runtime-os-candidate.yml pr-own-changes.txt" in text
    assert '--name-only "${BASE_SHA}...${HEAD_SHA}" > pr-own-changes.txt' in text
    for job_id in ("preflight", "review-evidence"):
        first_runs = [s.get("run", "") for s in jobs[job_id]["steps"] if "run" in s]
        assert 'find "$GITHUB_WORKSPACE" -mindepth 1 -maxdepth 1' in first_runs[
            0 if job_id == "review-evidence" else 1
        ]
    assert waiter["steps"][0]["env"]["CANDIDATE_WORKFLOW"] == CANDIDATE.name
    aggregate = jobs["hermes-required"]["steps"][0]["run"]
    assert 'test "$CANDIDATE" = success' in aggregate
    assert 'test "$CANDIDATE" = skipped' in aggregate


def test_candidate_workflow_is_unprivileged_and_mirrors_trusted_proof() -> None:
    workflow = yaml.safe_load(CANDIDATE.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"pull_request"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["run-name"] == (
        "Runtime OS candidate ${{ github.event.pull_request.head.sha }} "
        "on ${{ github.event.pull_request.base.sha }}"
    )
    assert all(job["runs-on"] == "ubuntu-latest" for job in workflow["jobs"].values())
    text = CANDIDATE.read_text(encoding="utf-8")
    assert "secrets." not in text
    assert "actions/cache/save" not in text
    assert "Upload protected-main duration sample" not in text
    assert "runtime-os-duration-${{" not in text
    trusted, candidate = _jobs(WORKFLOW), _jobs(CANDIDATE)
    assert candidate["plan"]["if"] == "github.event.pull_request.head.repo.full_name == github.repository"
    def as_candidate(value: object) -> object:
        # The trusted copy names its planner "preflight" and also gates on
        # non-PR events; otherwise the candidate copy must match it exactly.
        text = yaml.safe_dump(value, sort_keys=True)
        text = text.replace(f"!contains({PR_EVENTS}, github.event_name) &&", "")
        text = text.replace("preflight", "plan")
        return yaml.safe_load(text)

    for job_id in ("environment", "test", "e2e"):
        ours, theirs = candidate[job_id], trusted[job_id]
        assert ours["name"] == theirs["name"]
        assert "permissions" not in ours
        assert ours["runs-on"] == "ubuntu-latest"
        for key in ("needs", "timeout-minutes", "strategy"):
            assert ours.get(key) == as_candidate(theirs.get(key)), (job_id, key)
        trusted_if = " ".join(str(theirs["if"]).replace("${{", "").replace("}}", "").split())
        trusted_if = trusted_if.replace(f"!contains({PR_EVENTS}, github.event_name) && ", "")
        assert " ".join(str(ours["if"]).split()) == trusted_if.replace("preflight", "plan"), job_id
        trusted_steps = [s for s in theirs["steps"] if "protected-main" not in s.get("name", "")]
        for theirs_step, ours_step in zip(trusted_steps, ours["steps"], strict=True):
            if str(ours_step.get("uses", "")).startswith("actions/checkout"):
                assert ours_step["with"]["ref"] == "${{ github.event.pull_request.head.sha }}"
                continue
            if ours_step.get("name") == "Build locked environment with one infra-only retry":
                ours_step = dict(ours_step)
                assert ours_step.pop("env") == {
                    "UV_PYTHON_INSTALL_DIR": "${{ github.workspace }}/ci-fast/bin/.python",
                    "UV_PYTHON_PREFERENCE": "only-managed",
                }
            assert ours_step == as_candidate(theirs_step), (job_id, ours_step.get("name"))
