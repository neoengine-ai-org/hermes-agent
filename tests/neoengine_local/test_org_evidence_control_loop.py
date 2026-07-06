import json
from pathlib import Path

from neoengine_local.org_evidence import OrgEvidenceFabric, write_agent_closeout_receipt
from scripts.hermes_progress import _load_policies


def policy(org: str, pr: int = 1, *, check: str = "CI") -> dict:
    return {
        "schema": "org.policy.v1",
        "org": org,
        "policy_version": "test.v1",
        "critical_path": [
            {
                "kind": "pull_request",
                "number": pr,
                "reason": f"{org} critical path",
                "required_ladder": "roadmap_os_delivery.v1",
                "required_claims": ["CURRENT_HEAD_SIDECAR_RECEIPT_BOUND"],
                "eligible_lanes": [f"{org.upper()}-LANE-01"],
            }
        ],
        "required_checks": [check],
        "invalid_lanes": ["CEO", "founder"],
        "aftercare_requirements": ["POST_MERGE_VALIDATED"],
    }


def live(pr: int = 1, head: str = "h1", base: str = "b1", check: str = "CI", status: str = "SUCCESS", state: str = "OPEN") -> dict:
    return {"pull_requests": {pr: {"head_sha": head, "base_sha": base, "state": state, "checks": {check: status}}}}


def read(path: Path) -> dict:
    return json.loads(path.read_text())


def write_receipt(root: Path, org: str, claim: str, *, pr: int = 1, head: str = "h1", base: str = "b1", lane: str | None = None, evidence: dict | None = None) -> None:
    write_agent_closeout_receipt(
        root,
        org=org,
        repo=f"example/{org}",
        lane=lane or f"{org.upper()}-LANE-01",
        agent="sonnet",
        work_packet="packet",
        subject={"type": "pull_request", "number": pr, "head_sha": head, "base_sha": base},
        claims=[{"type": claim, "status": "candidate", "evidence": evidence or {"head_sha": head, "base_sha": base}}],
        non_claims=["not_live", "not_deployed", "not_accepted", "not_landed"],
    )


def test_synthetic_future_org_policy_drives_all_control_loop_outputs(tmp_path: Path) -> None:
    policies = {"neoengine": policy("neoengine", 726), "futureorg": policy("futureorg", 44)}
    state = {"neoengine": live(726), "futureorg": live(44)}
    OrgEvidenceFabric(tmp_path, policies=policies).verify_all(live_state=state)

    summary = read(tmp_path / "projections/all-orgs/watchdog-summary.json")
    gates = read(tmp_path / "projections/all-orgs/release-gates.json")
    graph = read(tmp_path / "projections/all-orgs/progress-graph.json")
    scorecard = read(tmp_path / "projections/all-orgs/anti-theater-scorecard.json")
    memory = read(tmp_path / "projections/all-orgs/release-memory.json")

    assert {"neoengine", "futureorg"}.issubset(summary["orgs"])
    assert {item["org"] for item in gates["gates"]} >= {"neoengine", "futureorg"}
    assert any(node["id"] == "org:futureorg" for node in graph["nodes"])
    assert any(item["org"] == "futureorg" for item in scorecard["orgs"])
    assert "futureorg" in memory["orgs"]


def test_policy_validation_errors_become_policy_debt_and_missing_schema_fails_closed(tmp_path: Path) -> None:
    bad_policy = {"org": "badorg", "critical_path": []}
    result = OrgEvidenceFabric(tmp_path, policies={"badorg": bad_policy}).verify_all(live_state={})

    assert result["badorg"]["proof_debt"] >= 1
    debt = read(tmp_path / "projections/badorg/proof-debt.json")
    assert any(item["debt_type"] == "POLICY_SCHEMA_INVALID" for item in debt["items"])
    gates = read(tmp_path / "projections/badorg/release-gates.json")
    assert all(gate["result"] == "fail" for gate in gates["gates"])


def test_event_idempotency_projection_hash_and_replay_are_stable(tmp_path: Path) -> None:
    policies = {"neoengine": policy("neoengine", 726)}
    state = {"neoengine": live(726)}
    write_receipt(tmp_path, "neoengine", "PR_REBASED", pr=726)
    OrgEvidenceFabric(tmp_path, policies=policies).verify_all(live_state=state)
    first = read(tmp_path / "projections/neoengine/product-progress-proof.json")

    # Same receipt semantics again must not duplicate verified progress.
    write_receipt(tmp_path, "neoengine", "PR_REBASED", pr=726)
    OrgEvidenceFabric(tmp_path, policies=policies).verify_all(live_state=state)
    second = read(tmp_path / "projections/neoengine/product-progress-proof.json")

    assert first["verified_movement"] == second["verified_movement"] == 1
    assert second["source_ledger_hash"].startswith("sha256:")
    assert second["projection_hash"].startswith("sha256:")
    replay = OrgEvidenceFabric(tmp_path, policies=policies).replay_projection("neoengine")
    assert replay["verified_movement"] == second["verified_movement"]


def test_graph_indexes_release_gate_and_no_dark_green_explain_blocked_state(tmp_path: Path) -> None:
    policies = {"neoengine": policy("neoengine", 726)}
    state = {"neoengine": live(726, status="CANCELLED")}
    OrgEvidenceFabric(tmp_path, policies=policies).verify_all(live_state=state)

    gate = read(tmp_path / "projections/neoengine/release-gates.json")["gates"][0]
    assert gate["result"] == "fail"
    assert "accepted" in gate["forbidden_claims"]
    assert gate["next_required_artifact"]
    assert gate["explainability"]["source_ledger_hash"] is not None

    by_pr = read(tmp_path / "projections/neoengine/indexes/by-critical-path.json")
    assert "pull_request:726" in by_pr
    graph = read(tmp_path / "projections/neoengine/progress-graph.json")
    assert any(edge["type"] == "proof_debt_blocks_release_gate" for edge in graph["edges"])


def test_dispatch_assignment_lifecycle_rejects_invalid_lanes_and_does_not_imply_readiness(tmp_path: Path) -> None:
    policies = {"neoengine": policy("neoengine", 726)}
    state = {"neoengine": live(726)}
    fabric = OrgEvidenceFabric(tmp_path, policies=policies)
    fabric.verify_all(live_state=state)
    tickets = read(tmp_path / "projections/all-orgs/dispatch-tickets.json")["tickets"]
    ticket_id = tickets[0]["ticket_id"]

    rejected = fabric.assign_dispatch_ticket(ticket_id, "CEO")
    assert rejected["status"] == "rejected"
    assigned = fabric.assign_dispatch_ticket(ticket_id, "NEOENGINE-LANE-01")
    assert assigned["status"] == "assigned"
    state_projection = read(tmp_path / "projections/all-orgs/dispatch-state.json")
    assert state_projection["assigned"] >= 1
    assert read(tmp_path / "projections/neoengine/release-readiness.json")["ready"] is False

    # Sidecar evidence closes the sidecar debt/ticket on the next verifier pass.
    write_receipt(tmp_path, "neoengine", "CURRENT_HEAD_SIDECAR_RECEIPT_BOUND", pr=726, evidence={"sidecar_receipt": "sidecar.json", "diff_hash": "diff1", "head_sha": "h1"})
    fabric.verify_all(live_state=state)
    closed = fabric.close_dispatch_ticket(ticket_id, evidence_event_required=False, reason="superseded_by_verified_evidence")
    assert closed["status"] == "closed"


def test_watchdog_views_material_notifications_aftercare_and_release_memory(tmp_path: Path) -> None:
    policies = {"neoengine": policy("neoengine", 726)}
    state = {"neoengine": live(726, state="MERGED")}
    write_receipt(tmp_path, "neoengine", "PR_REBASED", pr=726)
    OrgEvidenceFabric(tmp_path, policies=policies).verify_all(live_state=state)

    views = read(tmp_path / "projections/all-orgs/watchdog-views.json")
    assert {"founder", "ceo", "cto", "conductor", "agent"}.issubset(views["views"])
    assert "raw_candidates" not in json.dumps(views).lower()

    notifications = read(tmp_path / "projections/all-orgs/material-notifications.json")
    assert notifications["material_events"]
    assert any("proof debt" in item["summary"].lower() or "aftercare" in item["summary"].lower() for item in notifications["material_events"])
    aftercare = read(tmp_path / "projections/all-orgs/merge-aftercare.json")
    assert any(item["org"] == "neoengine" for item in aftercare["items"])
    memory = read(tmp_path / "projections/all-orgs/release-memory.json")
    assert memory["questions"]["which_prs_merged_without_aftercare"]


def test_dry_run_does_not_mutate_ledger_and_optional_policy_fields_are_tolerated(tmp_path: Path) -> None:
    rich_policy = policy("futureorg", 44) | {"optional_comment": "allowed"}
    write_receipt(tmp_path, "futureorg", "PR_REBASED", pr=44)
    fabric = OrgEvidenceFabric(tmp_path, policies={"futureorg": rich_policy})
    result = fabric.verify_all(live_state={"futureorg": live(44)}, dry_run=True)

    assert result["futureorg"]["promoted"] == 1
    assert not (tmp_path / "events/futureorg/events.jsonl").exists()

    real = fabric.verify_all(live_state={"futureorg": live(44)})
    assert real["futureorg"]["promoted"] == 1
    assert (tmp_path / "events/futureorg/events.jsonl").exists()


def test_policy_loader_accepts_temp_future_policy_without_hardcoded_org(tmp_path: Path) -> None:
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "futureorg.policy.json").write_text(json.dumps(policy("futureorg", 44)))

    loaded = _load_policies([str(policy_dir / "futureorg.policy.json")])

    assert loaded["futureorg"]["org"] == "futureorg"


def test_unknown_dispatch_ticket_fails_closed_without_assignment_file(tmp_path: Path) -> None:
    fabric = OrgEvidenceFabric(tmp_path, policies={"neoengine": policy("neoengine", 726)})

    result = fabric.assign_dispatch_ticket("ticket_does_not_exist", "NEOENGINE-LANE-01")
    close_result = fabric.close_dispatch_ticket("ticket_does_not_exist")

    assert result["status"] == "rejected"
    assert result["reason"] == "unknown_ticket_id"
    assert close_result["status"] == "rejected"
    assert close_result["reason"] == "unknown_ticket_id"
    assert not (tmp_path / "dispatch/assigned").exists()
    assert not (tmp_path / "dispatch/closed").exists()


def test_protected_boundary_receipt_blocks_gate_and_is_projected(tmp_path: Path) -> None:
    policies = {"neoengine": policy("neoengine", 726)}
    write_agent_closeout_receipt(
        tmp_path,
        org="neoengine",
        repo="example/neoengine",
        lane="NEOENGINE-LANE-01",
        agent="sonnet",
        work_packet="packet",
        subject={"type": "pull_request", "number": 726, "head_sha": "h1", "base_sha": "b1"},
        claims=[{"type": "PR_REBASED", "status": "candidate", "evidence": {"head_sha": "h1", "base_sha": "b1"}}],
        non_claims=["not_live"],
        protected_boundary=True,
        human_decision_required=True,
    )

    OrgEvidenceFabric(tmp_path, policies=policies).verify_all(live_state={"neoengine": live(726)})

    protected = read(tmp_path / "projections/all-orgs/protected-boundaries.json")
    gate = read(tmp_path / "projections/neoengine/release-gates.json")["gates"][0]
    assert protected["items"]
    assert gate["result"] == "fail"
    assert "PROTECTED_BOUNDARY_HIT" in gate["blocking_reasons"]
