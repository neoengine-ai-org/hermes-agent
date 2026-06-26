import json
from pathlib import Path

import pytest

from neoengine_local.org_evidence import OrgEvidenceFabric, write_agent_closeout_receipt


def _policy(org: str, pr: int = 726) -> dict:
    return {
        "schema": "org.policy.v1",
        "org": org,
        "critical_path": [
            {
                "kind": "pull_request",
                "number": pr,
                "required_ladder": "roadmap_os_delivery.v1",
                "required_claims": ["CURRENT_HEAD_SIDECAR_RECEIPT_BOUND"],
                "eligible_lanes": ["NE-SONNET-01", "NE-SONNET-02"],
            }
        ],
        "required_checks": ["Mac App"],
    }



def test_forbidden_claims_are_rejected_and_do_not_make_release_ready(tmp_path: Path) -> None:
    root = tmp_path / "org-evidence"
    policy = _policy("neoengine")
    write_agent_closeout_receipt(
        root,
        org="neoengine",
        lane="NE-SONNET-01",
        agent="sonnet",
        work_packet="roadmap-os-726-closeout",
        repo="neoengine-ai-org/neoengine",
        subject={"type": "pull_request", "number": 726, "head_sha": "abc123", "base_sha": "base123"},
        claims=[{"type": "accepted", "status": "candidate", "evidence": {}}],
        non_claims=[],
    )

    result = OrgEvidenceFabric(root=root, policies={"neoengine": policy}).verify_all(
        live_state={
            "neoengine": {
                "pull_requests": {
                    "726": {
                        "head_sha": "abc123",
                        "base_sha": "base123",
                        "checks": {"Mac App": "SUCCESS"},
                    }
                }
            }
        }
    )

    assert result["neoengine"]["promoted"] == 0
    assert result["neoengine"]["rejected"] == 1
    readiness = json.loads((root / "projections/neoengine/release-readiness.json").read_text())
    assert readiness["ready"] is False
    assert "accepted" in readiness["forbidden_claims"]


def test_missing_live_state_and_required_checks_are_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "org-evidence"
    policy = _policy("neoengine")
    fabric = OrgEvidenceFabric(root=root, policies={"neoengine": policy})

    missing_result = fabric.verify_all(live_state={})
    assert missing_result["neoengine"]["proof_debt"]
    missing_debt = json.loads((root / "projections/neoengine/proof-debt.json").read_text())
    assert {item["debt_type"] for item in missing_debt["items"]} >= {"LIVE_PR_STATE_MISSING"}
    assert json.loads((root / "projections/neoengine/release-readiness.json").read_text())["ready"] is False

    write_agent_closeout_receipt(
        root,
        org="neoengine",
        lane="NE-SONNET-01",
        agent="sonnet",
        work_packet="roadmap-os-726-closeout",
        repo="neoengine-ai-org/neoengine",
        subject={"type": "pull_request", "number": 726, "head_sha": "abc123", "base_sha": "base123"},
        claims=[{"type": "CURRENT_HEAD_CHECKS_GREEN", "status": "candidate", "evidence": {}}],
        non_claims=["not_accepted", "not_landed"],
    )

    result = fabric.verify_all(
        live_state={
            "neoengine": {
                "pull_requests": {
                    "726": {
                        "head_sha": "abc123",
                        "base_sha": "base123",
                        "checks": {"Unit": "SUCCESS"},
                    }
                }
            }
        }
    )
    assert result["neoengine"]["promoted"] == 0
    assert result["neoengine"]["rejected"] == 1
    proof_debt = json.loads((root / "projections/neoengine/proof-debt.json").read_text())
    assert {item["debt_type"] for item in proof_debt["items"]} >= {"REQUIRED_CHECK_MISSING"}


def test_org_names_cannot_escape_evidence_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_agent_closeout_receipt(
            tmp_path / "org-evidence",
            org="../../escape",
            lane="NE-SONNET-01",
            agent="sonnet",
            work_packet="packet",
            repo="neoengine-ai-org/neoengine",
            subject={"type": "pull_request", "number": 1, "head_sha": "head", "base_sha": "base"},
            claims=[{"type": "PR_REBASED", "status": "candidate", "evidence": {}}],
            non_claims=[],
        )

def test_agent_receipt_is_candidate_until_verifier_promotes_exact_live_subject(tmp_path: Path):
    fabric = OrgEvidenceFabric(tmp_path, policies={"neoengine": _policy("neoengine")})
    receipt_path = write_agent_closeout_receipt(
        tmp_path,
        org="neoengine",
        repo="neoengine-ai-org/neoengine",
        lane="NE-SONNET-01",
        agent="sonnet",
        work_packet="roadmap-os-726-closeout",
        subject={"type": "pull_request", "number": 726, "head_sha": "abc123", "base_sha": "def456"},
        claims=[{"type": "PR_REBASED", "status": "candidate", "evidence": {"head_sha": "abc123", "base_sha": "def456"}}],
        non_claims=["not_merged", "not_live", "not_accepted", "not_landed"],
    )

    assert receipt_path.exists()
    assert not list((tmp_path / "events" / "neoengine").glob("**/*.jsonl"))

    result = fabric.verify_all(
        live_state={
            "neoengine": {
                "pull_requests": {
                    726: {
                        "head_sha": "abc123",
                        "base_sha": "def456",
                        "state": "OPEN",
                        "checks": {"Mac App": "SUCCESS"},
                    }
                }
            }
        }
    )

    assert result["neoengine"]["promoted"] == 1
    projection = json.loads((tmp_path / "projections" / "neoengine" / "product-progress-proof.json").read_text())
    assert projection["verified_movement"] == 1
    assert projection["candidate_progress"] == 0
    event = json.loads((tmp_path / "events" / "neoengine" / "events.jsonl").read_text().splitlines()[0])
    assert event["event_status"] == "verified"
    assert event["event_type"] == "PR_REBASED"
    assert event["subject"]["head_sha"] == "abc123"
    assert event["verifier"]["name"] == "hermes-progress-verifier"
    assert event["integrity"]["event_hash"].startswith("sha256:")


def test_old_head_receipt_becomes_stale_and_creates_current_head_proof_debt(tmp_path: Path):
    fabric = OrgEvidenceFabric(tmp_path, policies={"neoengine": _policy("neoengine")})
    write_agent_closeout_receipt(
        tmp_path,
        org="neoengine",
        repo="neoengine-ai-org/neoengine",
        lane="NE-SONNET-01",
        agent="sonnet",
        work_packet="roadmap-os-726-closeout",
        subject={"type": "pull_request", "number": 726, "head_sha": "old111", "base_sha": "def456"},
        claims=[{"type": "CURRENT_HEAD_CHECKS_GREEN", "status": "candidate", "evidence": {"sha": "old111"}}],
        non_claims=["not_accepted", "not_landed"],
    )

    result = fabric.verify_all(
        live_state={"neoengine": {"pull_requests": {726: {"head_sha": "new222", "base_sha": "def456", "state": "OPEN", "checks": {"Mac App": "SUCCESS"}}}}}
    )

    assert result["neoengine"]["stale"] == 1
    stale_files = list((tmp_path / "stale" / "neoengine").glob("*.json"))
    assert stale_files
    proof_debt = json.loads((tmp_path / "projections" / "neoengine" / "proof-debt.json").read_text())
    current_head_debt = [item for item in proof_debt["items"] if item["debt_type"] == "CURRENT_HEAD_RECEIPT_REQUIRED"]
    assert current_head_debt
    assert current_head_debt[0]["subject"]["current_head_sha"] == "new222"


def test_critical_path_policy_creates_sidecar_debt_for_each_org_without_required_receipt(tmp_path: Path):
    fabric = OrgEvidenceFabric(
        tmp_path,
        policies={"neoengine": _policy("neoengine", 726), "neowealth": _policy("neowealth", 625)},
    )

    result = fabric.verify_all(
        live_state={
            "neoengine": {"pull_requests": {726: {"head_sha": "neohead", "base_sha": "main1", "state": "OPEN", "checks": {"Mac App": "SUCCESS"}}}},
            "neowealth": {"pull_requests": {625: {"head_sha": "nwhead", "base_sha": "main2", "state": "OPEN", "checks": {"Mac App": "SUCCESS"}}}},
        }
    )

    assert result["neoengine"]["proof_debt"] == 1
    assert result["neowealth"]["proof_debt"] == 1
    neo_debt = json.loads((tmp_path / "projections" / "neoengine" / "proof-debt.json").read_text())
    nw_debt = json.loads((tmp_path / "projections" / "neowealth" / "proof-debt.json").read_text())
    assert neo_debt["items"][0]["debt_type"] == "CURRENT_HEAD_SIDECAR_RECEIPT_MISSING"
    assert nw_debt["items"][0]["debt_type"] == "CURRENT_HEAD_SIDECAR_RECEIPT_MISSING"
    assert neo_debt["items"][0]["eligible_lanes"] == ["NE-SONNET-01", "NE-SONNET-02"]


def test_cancelled_required_check_rejects_green_claim_and_records_contradiction(tmp_path: Path):
    fabric = OrgEvidenceFabric(tmp_path, policies={"neoengine": _policy("neoengine")})
    write_agent_closeout_receipt(
        tmp_path,
        org="neoengine",
        repo="neoengine-ai-org/neoengine",
        lane="NE-SONNET-01",
        agent="sonnet",
        work_packet="roadmap-os-726-closeout",
        subject={"type": "pull_request", "number": 726, "head_sha": "abc123", "base_sha": "def456"},
        claims=[{"type": "CURRENT_HEAD_CHECKS_GREEN", "status": "candidate", "evidence": {"sha": "abc123"}}],
        non_claims=["not_accepted", "not_landed"],
    )

    result = fabric.verify_all(
        live_state={"neoengine": {"pull_requests": {726: {"head_sha": "abc123", "base_sha": "def456", "state": "OPEN", "checks": {"Mac App": "CANCELLED"}}}}}
    )

    assert result["neoengine"]["rejected"] == 1
    contradiction = json.loads((tmp_path / "projections" / "neoengine" / "contradiction-ledger.json").read_text())
    assert contradiction["contradictions"] == 1
    assert contradiction["items"][0]["contradiction"]["conflicting_fact"] == "required_check_cancelled:Mac App"
    debt = json.loads((tmp_path / "projections" / "neoengine" / "proof-debt.json").read_text())
    assert any(item["debt_type"] == "REQUIRED_CHECK_CANCELLED" for item in debt["items"])


def test_sidecar_claim_requires_artifact_binding(tmp_path: Path) -> None:
    root = tmp_path / "org-evidence"
    write_agent_closeout_receipt(
        root,
        org="neoengine",
        lane="NE-SONNET-01",
        agent="sonnet",
        work_packet="roadmap-os-726-closeout",
        repo="neoengine-ai-org/neoengine",
        subject={"type": "pull_request", "number": 726, "head_sha": "abc123", "base_sha": "base123"},
        claims=[{"type": "CURRENT_HEAD_SIDECAR_RECEIPT_BOUND", "status": "candidate", "evidence": {}}],
        non_claims=["not_accepted", "not_landed"],
    )

    result = OrgEvidenceFabric(root=root, policies={"neoengine": _policy("neoengine")}).verify_all(
        live_state={"neoengine": {"pull_requests": {726: {"head_sha": "abc123", "base_sha": "base123", "state": "OPEN", "checks": {"Mac App": "SUCCESS"}}}}}
    )

    assert result["neoengine"]["promoted"] == 0
    debt = json.loads((root / "projections/neoengine/proof-debt.json").read_text())
    assert {item["debt_type"] for item in debt["items"]} >= {"SIDECAR_RECEIPT_ARTIFACT_MISSING"}


def test_base_sha_mismatch_creates_current_base_debt(tmp_path: Path) -> None:
    root = tmp_path / "org-evidence"
    write_agent_closeout_receipt(
        root,
        org="neoengine",
        lane="NE-SONNET-01",
        agent="sonnet",
        work_packet="roadmap-os-726-closeout",
        repo="neoengine-ai-org/neoengine",
        subject={"type": "pull_request", "number": 726, "head_sha": "abc123", "base_sha": "oldbase"},
        claims=[{"type": "PR_REBASED", "status": "candidate", "evidence": {}}],
        non_claims=["not_accepted", "not_landed"],
    )

    result = OrgEvidenceFabric(root=root, policies={"neoengine": _policy("neoengine")}).verify_all(
        live_state={"neoengine": {"pull_requests": {726: {"head_sha": "abc123", "base_sha": "newbase", "state": "OPEN", "checks": {"Mac App": "SUCCESS"}}}}}
    )

    assert result["neoengine"]["rejected"] == 1
    debt = json.loads((root / "projections/neoengine/proof-debt.json").read_text())
    assert {item["debt_type"] for item in debt["items"]} >= {"CURRENT_BASE_RECEIPT_REQUIRED"}


def test_subject_pr_number_cannot_escape_evidence_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_agent_closeout_receipt(
            tmp_path / "org-evidence",
            org="neoengine",
            lane="NE-SONNET-01",
            agent="sonnet",
            work_packet="packet",
            repo="neoengine-ai-org/neoengine",
            subject={"type": "pull_request", "number": "../../escaped", "head_sha": "head", "base_sha": "base"},
            claims=[{"type": "PR_REBASED", "status": "candidate", "evidence": {}}],
            non_claims=[],
        )
