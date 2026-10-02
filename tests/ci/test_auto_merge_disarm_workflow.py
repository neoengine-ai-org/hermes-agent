"""Tests for the disarm-only auto-merge reconciler workflow.

Executes the workflow's embedded node script against a fake GitHub API and
asserts structural invariants across every workflow file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "auto-merge-disarm-reconciler.yml"
REPO = "org/repo"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _load():
    return yaml.safe_load(WORKFLOW.read_text())


def _script() -> str:
    steps = _load()["jobs"]["disarm-auto-merge"]["steps"]
    run = next(s["run"] for s in steps if "run" in s)
    m = re.search(r"node <<'NODE'\n(.*?)\nNODE\s*$", run, re.S)
    assert m, "embedded node heredoc not found"
    return m.group(1)


class FakeGitHub:
    def __init__(self):
        self.url = ""
        self.tmp: Path | None = None
        self.prs = {}  # number -> dict(state, armed, sha)
        self.comments = {}  # number -> list of {body, user:{login,type}}
        self.calls = []
        self.disarm_fails = set()
        self.disarm_noop = set()  # disarm "succeeds" but stays armed
        self.list_fail_page = None
        self.list_pages = 0
        self.user_login = None  # None => /user 403 (app token); else PAT user
        self.post_fail_count = 0
        self.merge_on_reread = set()  # armed in the list, MERGED by the time it is re-read
        self.merge_on_disarm = set()  # auto-merge completes between the live read and the disable
        self.after_first_page = None
        self.merged = []  # dicts: number, mergedAt, sha, events[(type, time)]
        self.audit_fail = False
        self.rearm_each_list = set()  # re-armed every time a listing starts
        # Response mutators for malformed-but-schema-valid API responses.
        self.armed_list_mutate = None  # fn(connection dict) applied to every ArmedList page
        self.pr_mutate = None  # fn(pr dict) applied to every PrByNumber read-back
        self.audit_mutate = None  # fn(list of nodes) applied to every RecentMerged page
        self.audit_conn_mutate = None  # fn(connection dict) applied to every RecentMerged page

    def actor(self):
        if self.user_login:
            return {"login": self.user_login, "type": "User"}
        return {"login": "steward", "type": "Bot"}

    def pr_gql(self, n):
        p = self.prs[n]
        return {
            "id": f"PR_{n}", "number": n, "state": p["state"].upper(), "headRefOid": p["sha"],
            "autoMergeRequest": {"enabledAt": "t"} if p["armed"] else None,
            "comments": {"nodes": [
                {"body": c["body"], "author": {"login": c["user"]["login"]}}
                for c in self.comments.get(n, [])[-30:]]},
        }


def _handler(fake: FakeGitHub):
    class H(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 - matches the base signature
            pass

        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            fake.calls.append(("GET", u.path))
            if u.path == "/user":
                if fake.user_login:
                    return self._send(200, {"login": fake.user_login})
                return self._send(403, {"message": "forbidden"})
            self._send(404, {"message": "nf"})

        def _graphql(self, body):
            query = body["query"]
            v = body["variables"]
            assert "enablePullRequestAutoMerge" not in query and "mergePullRequest" not in query
            if "mutation Disarm" in query:
                n = int(v["id"].split("_")[1])
                fake.calls.append(("GQL", "Disarm"))
                if n in fake.disarm_fails:
                    return self._send(200, {"errors": [{"message": "denied"}], "data": None})
                if n not in fake.disarm_noop:
                    fake.prs[n]["armed"] = False
                if n in fake.merge_on_disarm:
                    fake.prs[n]["state"] = "merged"
                return self._send(200, {"data": {"disablePullRequestAutoMerge": {"pullRequest": {"number": n}}}})
            if "query PrByNumber" in query:
                fake.calls.append(("GQL", "PrByNumber"))
                n = v["number"]
                if n in fake.merge_on_reread:
                    fake.prs[n]["state"] = "merged"
                pr = fake.pr_gql(n) if n in fake.prs else None
                if pr is not None and fake.pr_mutate:
                    fake.pr_mutate(pr)
                return self._send(200, {"data": {"repository": {"pullRequest": pr}}})
            if "query RecentMerged" in query:
                fake.calls.append(("GQL", "RecentMerged"))
                if fake.audit_fail:
                    return self._send(200, {"errors": [{"message": "audit boom"}], "data": None})
                m = re.search(r"pullRequests\(states: MERGED, first: (\d+)", re.sub(r"\s+", " ", query))
                size = int(m.group(1)) if m else 50
                ordered = sorted(fake.merged, key=lambda x: x["updatedAt"], reverse=True)
                start = int(v.get("cursor") or 0)
                page, more = ordered[start:start + size], start + size < len(ordered)
                nodes = [{
                    "number": x["number"], "mergedAt": x["mergedAt"], "updatedAt": x["updatedAt"],
                    "headRefOid": x["sha"], "mergeCommit": {"oid": x["sha"]},
                    "timelineItems": {
                        "pageInfo": {"hasPreviousPage": x["has_previous"]},
                        "nodes": [{"__typename": t, "createdAt": c} for t, c in x["events"]]},
                } for x in page]
                if fake.audit_mutate:
                    fake.audit_mutate(nodes)
                conn = {"pageInfo": {"hasNextPage": more, "endCursor": str(start + size) if more else None},
                        "nodes": nodes}
                if fake.audit_conn_mutate:
                    fake.audit_conn_mutate(conn)
                return self._send(200, {"data": {"repository": {"pullRequests": conn}}})
            if "query ArmedList" in query:
                fake.calls.append(("GQL", "ArmedList"))
                fake.list_pages += 1
                for n in fake.rearm_each_list:
                    fake.prs[n]["armed"] = True
                if fake.list_fail_page == fake.list_pages:
                    return self._send(200, {"errors": [{"message": "boom"}], "data": None})
                after = int(v["cursor"]) if v["cursor"] else 0  # keyset cursor = last PR number
                opens = [n for n in sorted(fake.prs) if fake.prs[n]["state"] == "open" and n > after]
                page, more = opens[:100], len(opens) > 100
                nodes = [{k: fake.pr_gql(n)[k] for k in ("id", "number", "autoMergeRequest")} for n in page]
                conn = {
                    "pageInfo": {"hasNextPage": more, "endCursor": str(page[-1]) if page else None},
                    "nodes": nodes}
                if fake.armed_list_mutate:
                    fake.armed_list_mutate(conn)
                resp = {"data": {"repository": {"pullRequests": conn}}}
                if fake.after_first_page:
                    cb, fake.after_first_page = fake.after_first_page, None
                    cb()
                return self._send(200, resp)
            self._send(400, {"errors": [{"message": "unknown query"}]})

        def do_POST(self):
            u = urlparse(self.path)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
            if u.path == "/graphql":
                return self._graphql(body)
            fake.calls.append(("POST", u.path))
            m = re.fullmatch(rf"/repos/{REPO}/issues/(\d+)/comments", u.path)
            if m:
                if fake.post_fail_count > 0:
                    fake.post_fail_count -= 1
                    return self._send(502, {"message": "transient"})
                n = int(m.group(1))
                fake.comments.setdefault(n, []).append({"body": body["body"], "user": fake.actor()})
                return self._send(201, {"id": 1})
            self._send(404, {"message": "nf"})

    return H


@pytest.fixture
def gh(tmp_path):
    fake = FakeGitHub()
    srv = HTTPServer(("127.0.0.1", 0), _handler(fake))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    fake.url = f"http://127.0.0.1:{srv.server_port}"
    fake.tmp = tmp_path
    yield fake
    srv.shutdown()


def add_pr(fake, n, armed=True, state="open", sha=None):
    fake.prs[n] = {"state": state, "armed": armed, "sha": sha or f"{n:040x}"}


def run(fake, event="schedule", pr_event=None, pr_number=""):
    script = fake.tmp / "s.js"
    script.write_text(_script())
    ev = fake.tmp / "event.json"
    ev.write_text(json.dumps(pr_event or {}))
    env = {
        "PATH": os.environ["PATH"], "GH_TOKEN": "t", "REPOSITORY": REPO,
        "EVENT_NAME": event, "GITHUB_EVENT_PATH": str(ev), "PR_NUMBER": pr_number,
        "GITHUB_API_URL": fake.url, "GITHUB_GRAPHQL_URL": fake.url + "/graphql",
    }
    return subprocess.run(["node", str(script)], env=env, capture_output=True, text=True, timeout=60)


def pr_event(n):
    return {"pull_request": {"number": n}}


# ---- behavior ----

@needs_node
def test_sweep_disarms_armed_and_posts_one_receipt(gh):
    add_pr(gh, 1, armed=True)
    add_pr(gh, 2, armed=False)
    add_pr(gh, 3, armed=True)
    r = run(gh)
    assert r.returncode == 0, r.stderr
    assert not gh.prs[1]["armed"] and not gh.prs[3]["armed"]
    assert len(gh.comments[1]) == 1 and len(gh.comments[3]) == 1 and 2 not in gh.comments
    assert gh.prs[1]["sha"] in gh.comments[1][0]["body"]
    assert "founder merge decision" in gh.comments[1][0]["body"]
    assert not any(c[0] == "GET" and "/pulls" in c[1] for c in gh.calls)
    assert run(gh).returncode == 0           # idle re-run posts nothing
    assert len(gh.comments[1]) == 1


@needs_node
def test_every_run_is_repository_complete_event_names_other_pr(gh):
    # Event payload names PR 2 while PR 1 is armed: both must be disarmed.
    add_pr(gh, 1, armed=True)
    add_pr(gh, 2, armed=True)
    r = run(gh, event="pull_request_target", pr_event=pr_event(2))
    assert r.returncode == 0, r.stderr
    assert not gh.prs[1]["armed"] and not gh.prs[2]["armed"]


@needs_node
def test_event_pr_is_processed_first(gh):
    add_pr(gh, 1, armed=True)
    add_pr(gh, 2, armed=True)
    assert run(gh, event="pull_request_target", pr_event=pr_event(2)).returncode == 0
    order = [c for c in gh.calls if c[0] == "GQL" and c[1] == "Disarm"]
    assert len(order) == 2
    assert "PrByNumber" == [c[1] for c in gh.calls if c[0] == "GQL"][0]


@needs_node
def test_dispatch_with_number_is_also_repository_complete(gh):
    add_pr(gh, 1, armed=True)
    add_pr(gh, 5, armed=True)
    assert run(gh, event="workflow_dispatch", pr_number="5").returncode == 0
    assert not gh.prs[1]["armed"] and not gh.prs[5]["armed"]


@needs_node
def test_closed_unmerged_pr_is_skipped_silently(gh):
    add_pr(gh, 5, armed=True, state="closed")
    r = run(gh, event="workflow_dispatch", pr_number="5")
    assert r.returncode == 0 and gh.prs[5]["armed"] and 5 not in gh.comments
    assert "::error::" not in r.stdout + r.stderr


@needs_node
def test_merged_before_disarm_raises_drift_alarm(gh):
    add_pr(gh, 1, armed=True)
    gh.merge_on_reread.add(1)
    r = run(gh)
    assert r.returncode != 0
    assert "::error::PR #1 head " + gh.prs[1]["sha"] in r.stdout
    assert "native auto-merge merged before disarm" in r.stdout
    assert 1 not in gh.comments
    assert not any(c == ("GQL", "Disarm") for c in gh.calls)


@needs_node
def test_failed_disarm_exits_nonzero_but_continues(gh):
    add_pr(gh, 1, armed=True)
    add_pr(gh, 2, armed=True)
    gh.disarm_fails.add(1)
    assert run(gh).returncode != 0
    assert not gh.prs[2]["armed"] and 1 not in gh.comments


@needs_node
def test_readback_still_armed_is_failure_and_no_receipt(gh):
    add_pr(gh, 1, armed=True)
    gh.disarm_noop.add(1)
    assert run(gh).returncode != 0 and 1 not in gh.comments


@needs_node
def test_incomplete_listing_fails_closed(gh):
    for n in range(1, 131):
        add_pr(gh, n, armed=False)
    gh.list_fail_page = 2
    assert run(gh).returncode != 0


@needs_node
def test_cursor_pagination_covers_more_than_one_page(gh):
    for n in range(1, 231):
        add_pr(gh, n, armed=(n == 230))
    r = run(gh)
    assert r.returncode == 0, r.stderr
    assert not gh.prs[230]["armed"]


@needs_node
def test_churn_during_listing_does_not_skip_prs(gh):
    for n in range(1, 131):
        add_pr(gh, n, armed=(n == 120))
    gh.after_first_page = lambda: gh.prs[1].update(state="closed")
    assert run(gh).returncode == 0
    assert not gh.prs[120]["armed"]


@needs_node
def test_receipt_post_failure_fails_run_and_is_not_retried_later(gh):
    add_pr(gh, 1, armed=True)
    gh.post_fail_count = 1
    assert run(gh).returncode != 0            # disarmed, notice failed -> nonzero
    assert not gh.prs[1]["armed"] and 1 not in gh.comments
    assert run(gh).returncode == 0            # nothing retries the notice later
    assert 1 not in gh.comments


@needs_node
def test_pat_user_receipt_is_deduped_by_exact_login(gh):
    gh.user_login = "steward-pat"
    add_pr(gh, 1, armed=True)
    assert run(gh).returncode == 0
    assert gh.comments[1][0]["user"]["type"] == "User"
    gh.prs[1]["armed"] = True
    assert run(gh).returncode == 0
    assert len(gh.comments[1]) == 1


@needs_node
def test_user_unavailable_posts_without_dedupe_even_for_other_bots(gh):
    add_pr(gh, 1, armed=True)
    marker = f"<!-- hermes-auto-merge-disarm:head={gh.prs[1]['sha']} -->"
    gh.comments[1] = [{"body": marker, "user": {"type": "Bot", "login": "other"}}]
    assert run(gh).returncode == 0
    assert len(gh.comments[1]) == 2
    gh.prs[1]["armed"] = True
    assert run(gh).returncode == 0            # /user unavailable: no dedupe, duplicate is harmless
    assert len(gh.comments[1]) == 3


@needs_node
def test_spoofed_marker_from_other_login_does_not_suppress(gh):
    gh.user_login = "steward-pat"
    add_pr(gh, 1, armed=True)
    marker = f"<!-- hermes-auto-merge-disarm:head={gh.prs[1]['sha']} -->"
    gh.comments[1] = [{"body": marker, "user": {"type": "User", "login": "mallory"}}]
    assert run(gh).returncode == 0
    assert len(gh.comments[1]) == 2


# ---- structure ----

def test_triggers_and_concurrency():
    wf = _load()
    on = wf.get("on") or wf[True]
    assert set(on["pull_request_target"]["types"]) == {
        "opened", "reopened", "synchronize", "ready_for_review", "labeled",
        "edited", "auto_merge_enabled", "converted_to_draft"}
    assert on["schedule"][0]["cron"] == "*/15 * * * *"
    assert "workflow_dispatch" in on
    assert wf["concurrency"]["cancel-in-progress"] is False
    # one repository-wide group: sweep and event runs are serialized
    assert wf["concurrency"]["group"] == "hermes-auto-merge-disarm"
    assert "pull_request" not in wf["concurrency"]["group"]
    # contents: write is needed by disablePullRequestAutoMerge with the github.token fallback
    assert wf["permissions"] == {"contents": "write", "pull-requests": "write", "issues": "write"}


ARM_MERGE_PATTERNS = [
    r"enablePullRequestAutoMerge",
    r"mergePullRequest",
    r"gh\s+pr\s+merge",
    r"pulls\.merge",
    r"pulls/\S*/merge\b",
    r"gh\s+api\b[^\n]*/merge\b",
    r"\"?/merge\"?\s*[,)]",
    r"pascalgn/automerge-action",
    r"peter-evans/enable-pull-request-automerge",
    r"ahmadnassri/action-dependabot-auto-merge",
    r"reitermarkus/automerge",
    r"auto-arm",
    # approval paths: a disarm-only workflow never reviews, let alone approves
    r"addPullRequestReview",
    r"submitPullRequestReview",
    r"pulls/\S*/reviews\b",
    r"pulls\.createReview",
    r"pulls\.submitReview",
    r"gh\s+pr\s+review\b",
    r"\bevent\s*[:=]\s*['\"]?APPROVE\b",
]


def _scan_files():
    files = list(WORKFLOWS.glob("*.y*ml"))
    actions = ROOT / ".github" / "actions"
    files += [p for p in actions.rglob("*") if p.is_file()]
    return files


def test_no_arm_or_merge_in_any_workflow_or_local_action():
    pats = [re.compile(p) for p in ARM_MERGE_PATTERNS]
    files = _scan_files()
    assert len(files) > 5
    for f in files:
        text = f.read_text(errors="replace")
        for pat in pats:
            assert not pat.search(text), f"{f.relative_to(ROOT)} matches {pat.pattern}"
    assert not (WORKFLOWS / "auto-arm-auto-merge.yml").exists()


def test_arm_merge_patterns_detect_known_bad_forms():
    samples = [
        "gh pr merge 1 --auto", "gh api -X PUT repos/o/r/pulls/1/merge",
        "github.rest.pulls.merge({})", "mergePullRequest(input:{})",
        "uses: pascalgn/automerge-action@v0", "enablePullRequestAutoMerge(",
        "peter-evans/enable-pull-request-automerge@v3",
        "ahmadnassri/action-dependabot-auto-merge@v2",
        "request('PUT /repos/o/r/pulls/1/merge')",
        # approval paths
        "addPullRequestReview(input: { pullRequestId: $id, event: APPROVE })",
        "submitPullRequestReview(input: { pullRequestReviewId: $id, event: APPROVE })",
        "gh api -X POST repos/o/r/pulls/1/reviews -f event=APPROVE",
        "request('POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews', { event: 'APPROVE' })",
        "github.rest.pulls.createReview({ owner, repo, pull_number: 1, event: 'APPROVE' })",
        "octokit.pulls.submitReview({ review_id: 2, event: 'APPROVE' })",
        "gh pr review 1 --approve",
        "gh pr review 1 -a",
    ]
    for sample in samples:
        assert any(re.search(p, sample) for p in ARM_MERGE_PATTERNS), sample


def test_every_uses_is_pinned_to_a_full_sha():
    text = WORKFLOW.read_text()
    uses = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", text, re.M)
    assert uses, "expected at least the app-token action"
    for ref in uses:
        if ref.startswith("./"):
            continue
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), f"unpinned action: {ref}"


def test_no_pr_head_checkout_and_no_untrusted_interpolation():
    wf = _load()
    text = WORKFLOW.read_text()
    assert "actions/checkout" not in text
    assert "github.event.pull_request.head" not in text
    for step in wf["jobs"]["disarm-auto-merge"]["steps"]:
        run_body = step.get("run", "")
        assert "${{" not in run_body, "expression interpolated into run script"
    for bad in ("title", "body", "head.ref", "head_ref", "labels", "github.event.pull_request.user"):
        assert f"github.event.pull_request.{bad}" not in text


def test_script_never_references_arm_mutation():
    assert "enablePullRequestAutoMerge" not in _script()
    assert "disablePullRequestAutoMerge" in _script()


def test_repair_machinery_is_gone():
    script = _script()
    for gone in ("repairReceipt", "paginateRest", "minCreatedAt"):
        assert gone not in script


@needs_node
def test_pr_armed_after_listing_is_caught_by_next_pass(gh):
    add_pr(gh, 1, armed=True)
    add_pr(gh, 2, armed=False)
    gh.after_first_page = lambda: gh.prs[2].update(armed=True)  # arms after pass-1 snapshot
    r = run(gh)
    assert r.returncode == 0, r.stderr
    assert not gh.prs[1]["armed"] and not gh.prs[2]["armed"]


@needs_node
def test_pr_rearmed_every_pass_fails_after_three_passes(gh):
    add_pr(gh, 1, armed=True)
    gh.rearm_each_list.add(1)
    r = run(gh)
    assert r.returncode != 0
    assert "::error::auto-merge still armed after 3 passes: #1" in r.stdout
    assert sum(1 for c in gh.calls if c == ("GQL", "Disarm")) == 3


@needs_node
def test_invalid_dispatch_input_still_sweeps_then_fails(gh):
    add_pr(gh, 1, armed=True)
    for bad in ("abc", "5; rm -rf"):
        gh.prs[1]["armed"] = True
        r = run(gh, event="workflow_dispatch", pr_number=bad)
        assert r.returncode != 0
        assert not gh.prs[1]["armed"]


def _iso(delta):
    return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_merged(fake, n, ago, events, updated_ago=None, has_previous=False):
    """events: list of (type, minutes-before-merge); merge itself is the MergedEvent."""
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    now = datetime.now(timezone.utc)
    merged = now - ago
    updated = now - (updated_ago if updated_ago is not None else ago)
    evs = [(t, (merged - timedelta(minutes=m)).strftime(fmt)) for t, m in events]
    evs.append(("MergedEvent", merged.strftime(fmt)))
    fake.merged.append({"number": n, "mergedAt": merged.strftime(fmt), "updatedAt": updated.strftime(fmt),
                        "sha": f"{n:040x}", "events": evs, "has_previous": has_previous})


@needs_node
def test_recent_native_auto_merge_raises_alarm_without_trigger(gh):
    add_merged(gh, 9, timedelta(minutes=10), [("AutoMergeEnabledEvent", 5)])
    r = run(gh)  # scheduled sweep, nothing armed
    assert r.returncode != 0
    assert f"::error::PR #9 merged via native auto-merge at {9:040x}; hermes grants no merge authority" in r.stdout
    assert not any(c[0] == "POST" for c in gh.calls)            # no comment
    assert not any(c == ("GQL", "Disarm") for c in gh.calls)    # no mutation


@needs_node
def test_enabled_then_disabled_before_manual_merge_is_not_alarmed(gh):
    add_merged(gh, 9, timedelta(minutes=10), [("AutoMergeEnabledEvent", 8), ("AutoMergeDisabledEvent", 6)])
    r = run(gh)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "::error::" not in r.stdout


@needs_node
def test_merged_pr_without_auto_merge_events_is_not_alarmed(gh):
    add_merged(gh, 9, timedelta(minutes=10), [])
    assert run(gh).returncode == 0


@needs_node
def test_merge_older_than_window_is_ignored(gh):
    add_merged(gh, 9, timedelta(hours=3), [("AutoMergeEnabledEvent", 5)])
    r = run(gh)
    assert r.returncode == 0 and "::error::" not in r.stdout


@needs_node
def test_failed_audit_query_is_nonzero_after_sweep_still_disarmed(gh):
    add_pr(gh, 1, armed=True)
    gh.audit_fail = True
    r = run(gh)
    assert r.returncode != 0
    assert not gh.prs[1]["armed"]
    assert any(c == ("GQL", "RecentMerged") for c in gh.calls)


@needs_node
def test_audit_runs_on_event_runs_too(gh):
    add_merged(gh, 9, timedelta(minutes=10), [("AutoMergeEnabledEvent", 5)])
    add_pr(gh, 1, armed=False)
    r = run(gh, event="pull_request_target", pr_event=pr_event(1))
    assert r.returncode != 0 and "::error::PR #9" in r.stdout


@needs_node
@pytest.mark.parametrize("enable_type", ["AutoSquashEnabledEvent", "AutoRebaseEnabledEvent"])
def test_squash_and_rebase_native_auto_merge_raise_alarm(gh, enable_type):
    add_merged(gh, 9, timedelta(minutes=10), [(enable_type, 5)])
    r = run(gh)
    assert r.returncode != 0
    assert f"::error::PR #9 merged via native auto-merge at {9:040x}" in r.stdout


@needs_node
def test_squash_enabled_then_disabled_before_manual_merge_is_not_alarmed(gh):
    add_merged(gh, 9, timedelta(minutes=10), [("AutoSquashEnabledEvent", 8), ("AutoMergeDisabledEvent", 6)])
    r = run(gh)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "::error::" not in r.stdout


@needs_node
def test_latest_enable_after_disable_alarms_for_squash(gh):
    add_merged(gh, 9, timedelta(minutes=10), [("AutoMergeDisabledEvent", 8), ("AutoSquashEnabledEvent", 6)])
    assert run(gh).returncode != 0


# ---- fail-closed on indeterminate API state (invariant d) ----

@needs_node
def test_armed_list_empty_pageinfo_fails_closed(gh):
    add_pr(gh, 1, armed=True)
    gh.armed_list_mutate = lambda conn: conn.__setitem__("pageInfo", {})
    r = run(gh)
    assert r.returncode != 0
    assert "pageInfo.hasNextPage is not a boolean" in r.stdout + r.stderr


@needs_node
@pytest.mark.parametrize("mutate", [
    lambda node: node.pop("autoMergeRequest"),
    lambda node: node.__setitem__("autoMergeRequest", "yes"),
    lambda node: node.__setitem__("number", "1"),
    lambda node: node.pop("id"),
], ids=["missing_autoMergeRequest", "string_autoMergeRequest", "string_number", "missing_id"])
def test_armed_list_malformed_node_fails_closed(gh, mutate):
    add_pr(gh, 1, armed=False)

    def apply(conn):
        for node in conn["nodes"]:
            mutate(node)
    gh.armed_list_mutate = apply
    r = run(gh)
    assert r.returncode != 0
    assert "open PR listing" in r.stdout + r.stderr


@needs_node
@pytest.mark.parametrize("mutate", [
    lambda pr: pr.pop("autoMergeRequest"),
    lambda pr: pr.__setitem__("autoMergeRequest", 1),
    lambda pr: pr.pop("state"),
    lambda pr: pr.__setitem__("state", "WEIRD"),
    lambda pr: pr.__setitem__("headRefOid", None),
], ids=["missing_autoMergeRequest", "numeric_autoMergeRequest", "missing_state", "unknown_state", "null_head"])
def test_pr_readback_unproven_state_fails_closed(gh, mutate):
    add_pr(gh, 5, armed=False)
    gh.pr_mutate = mutate
    r = run(gh, event="workflow_dispatch", pr_number="5")
    assert r.returncode != 0
    assert "indeterminate" in r.stdout + r.stderr


@needs_node
def test_merged_never_armed_triggering_pr_is_not_a_drift_alarm(gh):
    add_pr(gh, 5, armed=False, state="merged")
    r = run(gh, event="pull_request_target", pr_event=pr_event(5))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "::error::" not in r.stdout + r.stderr
    assert not any(c == ("GQL", "Disarm") for c in gh.calls)


@needs_node
@pytest.mark.parametrize("mutate", [
    lambda n: n.__setitem__("mergedAt", None),
    lambda n: n.pop("mergedAt"),
    lambda n: n.__setitem__("mergedAt", "not-a-date"),
    lambda n: n.__setitem__("updatedAt", None),
], ids=["mergedAt_null", "mergedAt_absent", "mergedAt_unparseable", "updatedAt_null"])
def test_audit_unproven_merge_time_fails_closed(gh, mutate):
    add_merged(gh, 9, timedelta(minutes=10), [])

    def apply(nodes):
        for n in nodes:
            mutate(n)
    gh.audit_mutate = apply
    r = run(gh)
    assert r.returncode != 0
    assert "not a parseable timestamp" in r.stdout + r.stderr


@needs_node
@pytest.mark.parametrize("mutate", [
    lambda n: n.__setitem__("timelineItems", None),
    lambda n: n["timelineItems"].__setitem__("nodes", None),
    lambda n: n["timelineItems"].pop("pageInfo"),
    lambda n: n["timelineItems"]["nodes"].append({"createdAt": "2026-01-01T00:00:00Z"}),
    lambda n: n["timelineItems"]["nodes"].append({"__typename": "AutoMergeDisabledEvent", "createdAt": None}),
], ids=["timeline_null", "nodes_null", "pageInfo_absent", "item_no_typename", "item_bad_createdAt"])
def test_audit_unproven_timeline_fails_closed(gh, mutate):
    add_merged(gh, 9, timedelta(minutes=10), [])

    def apply(nodes):
        for n in nodes:
            mutate(n)
    gh.audit_mutate = apply
    r = run(gh)
    assert r.returncode != 0
    assert "PR #9" in r.stdout + r.stderr


@needs_node
def test_audit_truncated_timeline_fails_closed(gh):
    add_merged(gh, 9, timedelta(minutes=10), [], has_previous=True)
    r = run(gh)
    assert r.returncode != 0
    assert "auto-merge timeline incomplete" in r.stdout + r.stderr


@needs_node
def test_audit_paginates_past_first_page_to_find_auto_merge(gh):
    for n in range(100, 150):  # 50 clean merges, updated more recently
        add_merged(gh, n, timedelta(minutes=5), [], updated_ago=timedelta(minutes=1))
    add_merged(gh, 9, timedelta(minutes=20), [("AutoMergeEnabledEvent", 5)])
    r = run(gh)
    assert r.returncode != 0
    assert f"::error::PR #9 merged via native auto-merge at {9:040x}" in r.stdout
    assert sum(1 for c in gh.calls if c == ("GQL", "RecentMerged")) == 2


@needs_node
def test_audit_stops_at_first_page_containing_node_older_than_window(gh):
    for n in range(100, 149):
        add_merged(gh, n, timedelta(minutes=5), [], updated_ago=timedelta(minutes=1))
    add_merged(gh, 200, timedelta(hours=5), [], updated_ago=timedelta(hours=3))
    add_merged(gh, 201, timedelta(hours=6), [("AutoMergeEnabledEvent", 5)], updated_ago=timedelta(hours=4))
    r = run(gh)
    assert r.returncode == 0, r.stdout + r.stderr
    assert sum(1 for c in gh.calls if c == ("GQL", "RecentMerged")) == 1


@needs_node
@pytest.mark.parametrize("page_info", [{}, {"hasNextPage": "false"}, {"hasNextPage": True, "endCursor": None}],
                         ids=["empty", "string_hasNextPage", "more_without_cursor"])
def test_audit_pageinfo_unproven_fails_closed(gh, page_info):
    add_merged(gh, 9, timedelta(minutes=10), [])
    gh.audit_conn_mutate = lambda conn: conn.__setitem__("pageInfo", page_info)
    r = run(gh)
    assert r.returncode != 0
    assert "recent-merge audit: " in r.stdout + r.stderr


@needs_node
def test_audit_malformed_node_fails_closed(gh):
    add_merged(gh, 9, timedelta(minutes=10), [])
    gh.audit_mutate = lambda nodes: nodes.append("not-an-object")
    r = run(gh)
    assert r.returncode != 0
    assert "recent-merge audit: malformed node" in r.stdout + r.stderr


@needs_node
def test_audit_updated_at_ordering_violation_fails_closed(gh):
    add_merged(gh, 9, timedelta(minutes=10), [])
    add_merged(gh, 10, timedelta(minutes=20), [])
    gh.audit_mutate = lambda nodes: nodes.reverse()
    r = run(gh)
    assert r.returncode != 0
    assert "updatedAt ordering violated" in r.stdout + r.stderr


@needs_node
def test_disable_recorded_at_merge_time_does_not_hide_enable(gh):
    # enable at T-2m; MergedEvent and AutoMergeDisabledEvent both at T-1m
    add_merged(gh, 9, timedelta(minutes=1), [("AutoMergeEnabledEvent", 1), ("AutoMergeDisabledEvent", 0)])
    r = run(gh)
    assert r.returncode != 0
    assert "::error::PR #9 merged via native auto-merge" in r.stdout


@needs_node
def test_enable_and_disable_tie_resolves_to_enabled(gh):
    add_merged(gh, 9, timedelta(minutes=10), [("AutoMergeEnabledEvent", 3), ("AutoMergeDisabledEvent", 3)])
    r = run(gh)
    assert r.returncode != 0
    assert "::error::PR #9 merged via native auto-merge" in r.stdout


@needs_node
def test_merged_event_created_after_merged_at_does_not_extend_the_disable_bound(gh):
    # mergedAt = T; enable at T-60s; disable at T+1s; MergedEvent object created at T+2s.
    # The disable is after the actual merge, so it must not hide the enable.
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    merged = datetime.now(timezone.utc) - timedelta(minutes=10)
    add_merged(gh, 9, timedelta(minutes=10), [])
    gh.merged[-1]["events"] = [
        ("AutoMergeEnabledEvent", (merged - timedelta(seconds=60)).strftime(fmt)),
        ("AutoMergeDisabledEvent", (merged + timedelta(seconds=1)).strftime(fmt)),
        ("MergedEvent", (merged + timedelta(seconds=2)).strftime(fmt)),
    ]
    gh.merged[-1]["mergedAt"] = merged.strftime(fmt)
    r = run(gh)
    assert r.returncode != 0
    assert "::error::PR #9 merged via native auto-merge" in r.stdout


@needs_node
def test_audit_updated_at_before_merged_at_fails_closed(gh):
    # updatedAt < mergedAt breaks the pagination completeness argument.
    add_merged(gh, 9, timedelta(minutes=10), [], updated_ago=timedelta(minutes=20))
    r = run(gh)
    assert r.returncode != 0
    assert "updatedAt precedes mergedAt (indeterminate)" in r.stdout + r.stderr


@needs_node
def test_merge_between_live_read_and_disable_is_a_drift_alarm_not_a_disarm(gh):
    add_pr(gh, 1, armed=True)
    gh.merge_on_disarm.add(1)
    r = run(gh)
    assert r.returncode != 0
    assert "native auto-merge merged before disarm" in r.stdout
    assert "ok: disarmed auto-merge for PR #1" not in r.stdout
    assert 1 not in gh.comments


def test_audit_query_shape():
    compact = re.sub(r"\s+", " ", _script())
    for needle in (
        "pullRequests(states: MERGED, first: ${AUDIT_PAGE_SIZE}, after: $cursor, orderBy: { field: UPDATED_AT, direction: DESC })",
        "pageInfo { hasNextPage endCursor }",
        "number mergedAt updatedAt headRefOid mergeCommit { oid }",
        "timelineItems(itemTypes: [AUTO_MERGE_ENABLED_EVENT, AUTO_SQUASH_ENABLED_EVENT, AUTO_REBASE_ENABLED_EVENT, AUTO_MERGE_DISABLED_EVENT, MERGED_EVENT], last: 100)",
        "pageInfo { hasPreviousPage }",
        "... on AutoSquashEnabledEvent { createdAt }",
        "... on AutoRebaseEnabledEvent { createdAt }",
    ):
        assert needle in compact, f"audit query shape changed: {needle}"
