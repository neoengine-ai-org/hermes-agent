import json
from pathlib import Path

from scripts import hermes_progress
from neoengine_local.org_evidence import write_agent_closeout_receipt


def test_cli_policy_list_and_validate_include_seeded_orgs(capsys):
    assert hermes_progress.main(["policy", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert {"neoengine", "neowealth", "everarc"}.issubset(listed["orgs"])
    assert hermes_progress.main(["policy", "validate"]) == 0


def test_cli_dry_run_verify_does_not_mutate_events(tmp_path: Path, capsys):
    policy = tmp_path / "future.policy.json"
    policy.write_text(json.dumps({"schema": "org.policy.v1", "org": "futureorg", "critical_path": [{"kind": "pull_request", "number": 1, "eligible_lanes": ["FUTURE-LANE"]}], "required_checks": ["CI"]}))
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"futureorg": {"pull_requests": {"1": {"head_sha": "h", "base_sha": "b", "checks": {"CI": "SUCCESS"}}}}}))
    write_agent_closeout_receipt(tmp_path, org="futureorg", repo="x/future", lane="FUTURE-LANE", agent="sonnet", work_packet="packet", subject={"type": "pull_request", "number": 1, "head_sha": "h", "base_sha": "b"}, claims=[{"type": "PR_REBASED", "status": "candidate", "evidence": {}}], non_claims=["not_live"])

    assert hermes_progress.main(["verify", "--root", str(tmp_path), "--policy", str(policy), "--live-state", str(live), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["futureorg"]["promoted"] == 1
    assert not (tmp_path / "events/futureorg/events.jsonl").exists()


def test_cli_gate_returns_nonzero_on_blocking_gate(tmp_path: Path, capsys):
    policy = tmp_path / "future.policy.json"
    policy.write_text(json.dumps({"schema": "org.policy.v1", "org": "futureorg", "critical_path": [{"kind": "pull_request", "number": 1, "eligible_lanes": ["FUTURE-LANE"], "required_claims": ["CURRENT_HEAD_SIDECAR_RECEIPT_BOUND"]}], "required_checks": ["CI"]}))
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"futureorg": {"pull_requests": {"1": {"head_sha": "h", "base_sha": "b", "checks": {"CI": "CANCELLED"}}}}}))

    assert hermes_progress.main(["gate", "evaluate", "--root", str(tmp_path), "--policy", str(policy), "--live-state", str(live)]) == 2
    gates = json.loads(capsys.readouterr().out)
    assert gates["gates"][0]["result"] == "fail"


def test_cli_dispatch_rejects_invalid_lane_after_verify(tmp_path: Path, capsys):
    policy = tmp_path / "future.policy.json"
    policy.write_text(json.dumps({"schema": "org.policy.v1", "org": "futureorg", "critical_path": [{"kind": "pull_request", "number": 1, "eligible_lanes": ["FUTURE-LANE"], "required_claims": ["CURRENT_HEAD_SIDECAR_RECEIPT_BOUND"]}], "required_checks": ["CI"], "invalid_lanes": ["CEO"]}))
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"futureorg": {"pull_requests": {"1": {"head_sha": "h", "base_sha": "b", "checks": {"CI": "SUCCESS"}}}}}))
    assert hermes_progress.main(["verify", "--root", str(tmp_path), "--policy", str(policy), "--live-state", str(live)]) == 0
    capsys.readouterr()
    ticket_id = json.loads((tmp_path / "projections/all-orgs/dispatch-tickets.json").read_text())["tickets"][0]["ticket_id"]

    assert hermes_progress.main(["dispatch", "assign", "--root", str(tmp_path), "--policy", str(policy), ticket_id, "CEO"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "rejected"
