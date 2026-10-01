import os
import subprocess
import sys
from pathlib import Path

import pytest
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
    assert 'python scripts/run_tests_parallel.py -j 4 "${selected[@]}"' in text
    assert "scripts/run_tests.sh" not in text
    assert "--files" not in text
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
        cleanup = [
            s for s in jobs[job_id]["steps"]
            if s.get("name") == "Discard residue from earlier jobs on this shared runner"
        ]
        assert len(cleanup) == 1 and 'find "$GITHUB_WORKSPACE" -mindepth 1 -maxdepth 1' in cleanup[0]["run"]
        names = [s.get("name", "") for s in jobs[job_id]["steps"]]
        assert names.index(cleanup[0]["name"]) < min(
            i for i, s in enumerate(jobs[job_id]["steps"]) if str(s.get("uses", "")).startswith("actions/checkout")
        )
    assert waiter["steps"][0]["with"]["retries"] == 3
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
    assert workflow["env"]["UV_PYTHON_INSTALL_DIR"] == "${{ github.workspace }}/ci-fast/bin/.python"
    assert workflow["env"]["UV_PYTHON_PREFERENCE"] == "only-managed"
    text = CANDIDATE.read_text(encoding="utf-8")
    assert "test_wheel_locales_e2e.py" not in text
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
        hosted_only = [s for s in ours["steps"] if s.get("name") == "Install pinned uv for hosted consumers"]
        assert len(hosted_only) == (0 if job_id == "environment" else 1), job_id
        ours_steps = [s for s in ours["steps"] if s not in hosted_only]
        for theirs_step, ours_step in zip(trusted_steps, ours_steps, strict=True):

            if str(ours_step.get("uses", "")).startswith("actions/checkout"):
                assert ours_step["with"]["ref"] == "${{ github.event.pull_request.head.sha }}"
                continue
            assert ours_step == as_candidate(theirs_step), (job_id, ours_step.get("name"))


def test_restored_environment_keeps_receipt_checkout_clean_and_e2e_runnable() -> None:
    # run_tests.sh validates receipt-grade with `git status --untracked-files=all`;
    # the restored ci-fast/ tree lives in the checkout and must be ignored.
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/ci-fast/" in ignored
    for path in (WORKFLOW, CANDIDATE):
        e2e = _jobs(path)["e2e"]
        run = next(s for s in e2e["steps"] if s.get("name") == "Run full e2e proof")["run"]
        # tests/integration is all `integration`-marked (external services) and
        # deselected by addopts, so pytest exits 5; mirror tests.yml's e2e lane.
        assert "tests/integration" not in run, path.name
        assert "python -m pytest tests/e2e/ -v --tb=short" in run, path.name



def _step_run(path: Path, job_id: str, name: str) -> str:
    return next(s for s in _jobs(path)[job_id]["steps"] if s.get("name") == name)["run"]


def test_restored_environment_layout_passes_the_receipt_cleanliness_check(tmp_path: Path) -> None:
    # Behavioural: reproduce the exact restored layout inside a clean checkout
    # and run the validator's own cleanliness query against it.
    repo = tmp_path / "checkout"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run([*git[:3], "init", "-q"], check=True)
    (repo / ".gitignore").write_text((ROOT / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run([*git, "add", ".gitignore"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    for relative in (
        "ci-fast/hermes-ci-fast-environment.tar.gz",
        "ci-fast/bin/rg",
        "ci-fast/bin/.python/cpython-3.11.15-linux-x86_64-gnu/bin/python3.11",
        ".venv/bin/python",
        ".venv/pyvenv.cfg",
    ):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("restored", encoding="utf-8")
    status = ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"]
    assert subprocess.run(status, capture_output=True, text=True, check=True).stdout == ""
    # The invariant is not globally disabled: an unrelated stray file is dirty.
    (repo / "stray.py").write_text("x = 1\n", encoding="utf-8")
    assert "stray.py" in subprocess.run(status, capture_output=True, text=True, check=True).stdout
    validator = (ROOT / "scripts/validate_hermes_bootstrap_closure.py").read_text(encoding="utf-8")
    assert '"status", "--porcelain=v1", "--untracked-files=all"' in validator
    for workflow in (WORKFLOW, CANDIDATE):
        restore = _step_run(workflow, "test", "Restore immutable environment")
        assert "tar -xzf ci-fast/hermes-ci-fast-environment.tar.gz" in restore
        build = _step_run(workflow, "environment", "Build locked environment with one infra-only retry")
        assert "tar -czf ci-fast/hermes-ci-fast-environment.tar.gz .venv ci-fast/bin" in build


def _run_slice_script(workflow: Path, workdir: Path, record: Path) -> subprocess.CompletedProcess[str]:
    script = _step_run(workflow, "test", "Run selected files with interpreter isolation")
    fake_bin = workdir.parent / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    fake_python = fake_bin / "python"
    # Records every python invocation; the collect-only probe exits 0.
    fake_python.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> "$RECORD"\nprintf -- "--\\n" >> "$RECORD"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "SELECTED_FILES": "tests/a/test_x.py:tests/b/test_y.py",
        "RECORD": str(record),
    }
    return subprocess.run(["bash", "-e", "-c", script], cwd=workdir, env=env, capture_output=True, text=True)


def test_slice_script_enforces_clean_checkout_then_runs_selected_files(tmp_path: Path) -> None:
    # Execute each workflow's real slice script in a git checkout carrying the
    # restored layout, with a recording `python` on PATH.
    for workflow in (WORKFLOW, CANDIDATE):
        repo = tmp_path / workflow.stem / "checkout"
        repo.mkdir(parents=True)
        git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
        subprocess.run([*git[:3], "init", "-q"], check=True)
        (repo / ".gitignore").write_text((ROOT / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8")
        subprocess.run([*git, "add", ".gitignore"], check=True)
        subprocess.run([*git, "commit", "-qm", "base"], check=True)
        for relative in (".venv/bin/activate", "ci-fast/bin/rg", "ci-fast/hermes-ci-fast-environment.tar.gz"):
            (repo / relative).parent.mkdir(parents=True, exist_ok=True)
            (repo / relative).write_text("", encoding="utf-8")
        record = tmp_path / f"{workflow.stem}.record"
        result = _run_slice_script(workflow, repo, record)
        assert result.returncode == 0, result.stderr
        calls = [c.strip("\n").splitlines() for c in record.read_text(encoding="utf-8").split("--\n") if c.strip()]
        assert calls[-1] == [
            "scripts/run_tests_parallel.py",
            "-j",
            "4",
            "tests/a/test_x.py",
            "tests/b/test_y.py",
        ], workflow.name
        # A stray file makes the checkout dirty: the script fails before tests.
        (repo / "stray.py").write_text("x = 1\n", encoding="utf-8")
        record.unlink()
        result = _run_slice_script(workflow, repo, record)
        assert result.returncode != 0 and "DIRTY_CHECKOUT" in result.stderr, workflow.name
        # Nothing ran before the cleanliness check: not even collection.
        assert not record.exists(), workflow.name
        script = _step_run(workflow, "test", "Run selected files with interpreter isolation")
        assert script.index("git status --porcelain=v1") < script.index("source .venv/bin/activate")
    help_text = subprocess.run(
        [sys.executable, str(ROOT / "scripts/run_tests_parallel.py"), "--help"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "PATH" in help_text and "--files" not in help_text
    tests_yml = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    assert "python scripts/run_tests_parallel.py --slice" in tests_yml


def test_e2e_proof_selects_the_repository_e2e_suite() -> None:
    # tests.yml's required e2e lane runs exactly `pytest tests/e2e/`; the
    # proof must select a non-empty set from it under the repo's addopts
    # (integration-marked external-service tests stay deselected).
    for workflow in (WORKFLOW, CANDIDATE):
        run = _step_run(workflow, "e2e", "Run full e2e proof")
        assert [line.strip() for line in run.strip().splitlines()] == [
            "source .venv/bin/activate",
            "python -m pytest tests/e2e/ -v --tb=short",
        ]
    tests_yml = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    assert "python -m pytest tests/e2e/ -v --tb=short" in tests_yml
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/e2e/"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert collected.returncode == 0, collected.stdout + collected.stderr
    assert "no tests collected" not in collected.stdout
    assert sum("::" in line for line in collected.stdout.splitlines()) > 0


_WAITER_HARNESS = r"""
const fs = require('fs');
const scenario = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
let now = 1000000;
Date.now = () => now;
global.setTimeout = (fn, ms) => { now += ms; fn(); return 0; };
let poll = 0;
const calls = [];
const github = {
  rest: { actions: { listWorkflowRuns: 'listWorkflowRuns' } },
  paginate: async (endpoint, params) => {
    calls.push({ endpoint, params });
    const runs = scenario.polls[Math.min(poll, scenario.polls.length - 1)];
    poll += 1;
    return runs;
  },
};
const context = { repo: { owner: 'neoengine-ai-org', repo: 'hermes-agent' } };
const out = { failed: null, info: [] };
const core = { info: (m) => out.info.push(m), setFailed: (m) => { out.failed = m; } };
Object.assign(process.env, scenario.env);
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
new AsyncFunction('github', 'context', 'core', 'require', scenario.script)(github, context, core, require)
  .then(() => { out.calls = calls; out.polls = poll; console.log(JSON.stringify(out)); })
  .catch((error) => { console.error(error); process.exit(3); });
"""

_HEAD = "a" * 40
_BASE = "b" * 40


def _run(run_id: int, conclusion: str | None, **overrides: object) -> dict[str, object]:
    run: dict[str, object] = {
        "id": run_id,
        "head_sha": _HEAD,
        "event": "pull_request",
        "display_title": f"Runtime OS candidate {_HEAD} on {_BASE}",
        "head_repository": {"full_name": "neoengine-ai-org/hermes-agent"},
        "pull_requests": [{"number": 100}],
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion,
        "html_url": f"https://example.invalid/runs/{run_id}",
        "run_attempt": 1,
    }
    run.update(overrides)
    return run


def _waiter(tmp_path: Path, polls: list[list[dict[str, object]]], definition_changed: str = "false") -> dict:
    import json
    import shutil

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the github-script waiter")
    step = _jobs(WORKFLOW)["candidate-proof"]["steps"][0]
    env = {key: str(value) for key, value in step["env"].items() if "${{" not in str(value)}
    env.update(EXPECTED_HEAD=_HEAD, EXPECTED_PR="100", EXPECTED_BASE=_BASE, DEFINITION_CHANGED=definition_changed)
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"script": step["with"]["script"], "env": env, "polls": polls}), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(_WAITER_HARNESS, encoding="utf-8")
    result = subprocess.run([node, str(harness), str(scenario)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_waiter_accepts_only_the_exact_bound_successful_run(tmp_path: Path) -> None:
    out = _waiter(tmp_path, [[_run(1, "success")]])
    assert out["failed"] is None
    params = out["calls"][0]["params"]
    assert params["workflow_id"] == CANDIDATE.name
    assert params["event"] == "pull_request" and params["head_sha"] == _HEAD


@pytest.mark.parametrize(
    "runs",
    [
        [_run(1, "success", display_title=f"Runtime OS candidate {_HEAD} on {'c' * 40}")],
        [_run(1, "success", pull_requests=[{"number": 100}, {"number": 101}])],
        [_run(1, "success", pull_requests=[{"number": 101}])],
        [_run(1, "success", pull_requests=[])],
        [_run(1, "success", head_repository={"full_name": "attacker/hermes-agent"})],
        [_run(1, "success", head_sha="d" * 40)],
        [_run(1, "success", event="pull_request_target")],
    ],
)
def test_waiter_rejects_runs_not_bound_to_this_pr_head_and_base(tmp_path: Path, runs) -> None:
    out = _waiter(tmp_path, [runs])
    assert out["failed"] and "No candidate proof run bound to PR #100" in out["failed"]


def test_waiter_uses_the_latest_run_and_fails_on_its_failure(tmp_path: Path) -> None:
    out = _waiter(tmp_path, [[_run(1, "success"), _run(2, "failure")]])
    assert out["failed"] and "concluded failure" in out["failed"]


def test_waiter_lets_a_superseded_cancelled_run_settle(tmp_path: Path) -> None:
    out = _waiter(
        tmp_path,
        [[_run(1, "cancelled")], [_run(1, "cancelled"), _run(2, None)], [_run(1, "cancelled"), _run(2, "success")]],
    )
    assert out["failed"] is None and out["polls"] == 3


def test_waiter_fails_closed_when_the_pr_edits_the_candidate_definition(tmp_path: Path) -> None:
    for flag in ("true", ""):
        out = _waiter(tmp_path, [[_run(1, "success")]], definition_changed=flag)
        assert out["failed"] and "self-defined" in out["failed"]
        assert out["calls"] == []
