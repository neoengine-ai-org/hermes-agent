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
        self.after_first_page = None
        self.rearm_each_list = set()  # re-armed every time a listing starts

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
        def log_message(self, *a):
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
                return self._send(200, {"data": {"disablePullRequestAutoMerge": {"pullRequest": {"number": n}}}})
            if "query PrByNumber" in query:
                fake.calls.append(("GQL", "PrByNumber"))
                n = v["number"]
                if n in fake.merge_on_reread:
                    fake.prs[n]["state"] = "merged"
                pr = fake.pr_gql(n) if n in fake.prs else None
                return self._send(200, {"data": {"repository": {"pullRequest": pr}}})
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
                resp = {"data": {"repository": {"pullRequests": {
                    "pageInfo": {"hasNextPage": more, "endCursor": str(page[-1]) if page else None},
                    "nodes": nodes}}}}
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
    for gone in ("repairReceipt", "timelineItems", "AutoMergeDisabledEvent", "paginateRest"):
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
