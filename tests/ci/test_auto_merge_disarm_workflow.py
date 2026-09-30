"""Tests for the disarm-only auto-merge reconciler workflow.

Executes the workflow's embedded node script against a fake GitHub API and
asserts structural invariants across every workflow file.
"""

from __future__ import annotations

import json
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
        self.prs = {}  # number -> dict(state, armed, sha, node_id)
        self.comments = {}  # number -> list
        self.calls = []
        self.disarm_fails = set()
        self.disarm_noop = set()  # disarm "succeeds" but stays armed
        self.list_fail_page = None
        self.user_login = None  # None => /user 403

    def pr_json(self, n):
        p = self.prs[n]
        return {
            "number": n, "state": p["state"], "node_id": f"PR_{n}",
            "auto_merge": {"merge_method": "squash"} if p["armed"] else None,
            "head": {"sha": p["sha"]},
        }


def _handler(fake: FakeGitHub):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, headers=None):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            fake.calls.append(("GET", u.path))
            if u.path == "/user":
                if fake.user_login:
                    return self._send(200, {"login": fake.user_login})
                return self._send(403, {"message": "forbidden"})
            if u.path == f"/repos/{REPO}/pulls":
                page = int(q["page"][0])
                per = int(q["per_page"][0])
                if fake.list_fail_page == page:
                    return self._send(500, {"message": "boom"})
                opens = [fake.pr_json(n) for n in sorted(fake.prs) if fake.prs[n]["state"] == "open"]
                return self._send(200, opens[(page - 1) * per: page * per])
            m = re.fullmatch(rf"/repos/{REPO}/pulls/(\d+)", u.path)
            if m:
                return self._send(200, fake.pr_json(int(m.group(1))))
            m = re.fullmatch(rf"/repos/{REPO}/issues/(\d+)/comments", u.path)
            if m:
                cs = fake.comments.get(int(m.group(1)), [])
                page = int(q["page"][0]); per = int(q["per_page"][0])
                return self._send(200, cs[(page - 1) * per: page * per])
            self._send(404, {"message": "nf"})

        def do_POST(self):
            u = urlparse(self.path)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
            fake.calls.append(("POST", u.path))
            m = re.fullmatch(rf"/repos/{REPO}/issues/(\d+)/comments", u.path)
            if m:
                n = int(m.group(1))
                fake.comments.setdefault(n, []).append(
                    {"body": body["body"], "user": {"type": "Bot", "login": "steward[bot]"}})
                return self._send(201, {"id": 1})
            if u.path == "/graphql":
                query = body["query"]
                fake.calls.append(("GQL", query.split("(")[0].split("{")[0].strip()))
                pid = body["variables"]["id"]
                n = int(pid.split("_")[1])
                if "enablePullRequestAutoMerge" in query or "mergePullRequest" in query:
                    raise AssertionError("arm/merge mutation attempted")
                if "disablePullRequestAutoMerge" in query:
                    if n in fake.disarm_fails:
                        return self._send(200, {"errors": [{"message": "denied"}], "data": None})
                    if n not in fake.disarm_noop:
                        fake.prs[n]["armed"] = False
                    return self._send(200, {"data": {"disablePullRequestAutoMerge": {"pullRequest": {"number": n}}}})
                p = fake.prs[n]
                return self._send(200, {"data": {"node": {
                    "number": n, "headRefOid": p["sha"],
                    "autoMergeRequest": {"enabledAt": "t"} if p["armed"] else None}}})
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
        "PATH": __import__("os").environ["PATH"], "GH_TOKEN": "t", "REPOSITORY": REPO,
        "EVENT_NAME": event, "GITHUB_EVENT_PATH": str(ev), "PR_NUMBER": pr_number,
        "GITHUB_API_URL": fake.url, "GITHUB_GRAPHQL_URL": fake.url + "/graphql",
    }
    return subprocess.run(["node", str(script)], env=env, capture_output=True, text=True, timeout=60)


# ---- behavior ----

@needs_node
def test_sweep_disarms_armed_and_comments_once(gh):
    add_pr(gh, 1, armed=True)
    add_pr(gh, 2, armed=False)
    add_pr(gh, 3, armed=True)
    r = run(gh)
    assert r.returncode == 0, r.stderr
    assert not gh.prs[1]["armed"] and not gh.prs[3]["armed"]
    assert len(gh.comments[1]) == 1 and len(gh.comments[3]) == 1 and 2 not in gh.comments
    assert gh.prs[1]["sha"] in gh.comments[1][0]["body"]
    assert "founder merge decision" in gh.comments[1][0]["body"]
    # re-arm same head, rerun: no duplicate receipt
    gh.prs[1]["armed"] = True
    assert run(gh).returncode == 0
    assert len(gh.comments[1]) == 1
    # new head gets a new receipt
    gh.prs[1].update(armed=True, sha="f" * 40)
    assert run(gh).returncode == 0
    assert len(gh.comments[1]) == 2


@needs_node
def test_never_arms_or_merges(gh):
    add_pr(gh, 1, armed=False)
    add_pr(gh, 2, armed=True)
    assert run(gh).returncode == 0
    assert not any(c[0] == "GQL" and ("enable" in c[1].lower() or "merge" in c[1].lower() and "Disarm" not in c[1]) for c in gh.calls)


@needs_node
def test_event_run_handles_single_pr_using_live_state(gh):
    add_pr(gh, 1, armed=True)
    add_pr(gh, 2, armed=True)
    r = run(gh, event="pull_request_target", pr_event={"pull_request": {"number": 2, "auto_merge": None}})
    assert r.returncode == 0, r.stderr
    assert gh.prs[1]["armed"] and not gh.prs[2]["armed"]


@needs_node
def test_dispatch_with_number_and_closed_pr(gh):
    add_pr(gh, 5, armed=True, state="closed")
    r = run(gh, event="workflow_dispatch", pr_number="5")
    assert r.returncode == 0 and gh.prs[5]["armed"] and 5 not in gh.comments
    assert run(gh, event="workflow_dispatch", pr_number="5; rm -rf").returncode != 0


@needs_node
def test_failed_disarm_exits_nonzero_but_continues(gh):
    add_pr(gh, 1, armed=True)
    add_pr(gh, 2, armed=True)
    gh.disarm_fails.add(1)
    r = run(gh)
    assert r.returncode != 0
    assert not gh.prs[2]["armed"]
    assert 1 not in gh.comments


@needs_node
def test_readback_still_armed_is_failure_and_no_receipt(gh):
    add_pr(gh, 1, armed=True)
    gh.disarm_noop.add(1)
    r = run(gh)
    assert r.returncode != 0 and 1 not in gh.comments


@needs_node
def test_incomplete_listing_fails_closed(gh):
    for n in range(1, 131):
        add_pr(gh, n, armed=False)
    gh.list_fail_page = 2
    assert run(gh).returncode != 0


@needs_node
def test_pagination_covers_more_than_one_page(gh):
    for n in range(1, 131):
        add_pr(gh, n, armed=(n == 130))
    r = run(gh)
    assert r.returncode == 0, r.stderr
    assert not gh.prs[130]["armed"]


@needs_node
def test_spoofed_human_marker_does_not_suppress_receipt(gh):
    add_pr(gh, 1, armed=True)
    marker = f"<!-- hermes-auto-merge-disarm:head={gh.prs[1]['sha']} -->"
    gh.comments[1] = [{"body": marker, "user": {"type": "User", "login": "mallory"}}]
    assert run(gh).returncode == 0
    assert len(gh.comments[1]) == 2


@needs_node
def test_login_mismatch_bot_does_not_suppress_receipt(gh):
    add_pr(gh, 1, armed=True)
    gh.user_login = "steward[bot]"
    marker = f"<!-- hermes-auto-merge-disarm:head={gh.prs[1]['sha']} -->"
    gh.comments[1] = [{"body": marker, "user": {"type": "Bot", "login": "other[bot]"}}]
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
    assert "pull_request.number" in wf["concurrency"]["group"]
    assert wf["permissions"] == {"contents": "read", "pull-requests": "write", "issues": "write"}


def test_no_arm_or_merge_in_any_workflow():
    pat = re.compile(r"enablePullRequestAutoMerge|mergePullRequest|gh\s+pr\s+merge|pulls\.merge|--auto\b|auto-arm")
    for f in WORKFLOWS.glob("*.y*ml"):
        text = f.read_text()
        assert not pat.search(text), f"{f.name} contains an arm/merge path"
    assert not (WORKFLOWS / "auto-arm-auto-merge.yml").exists()


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
