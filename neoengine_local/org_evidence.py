"""Verified evidence fabric for org delivery claims.

Agents write candidate closeout receipts into an inbox.  The verifier is the
only component that promotes those claims into canonical evidence events and
derived projections.  This module is intentionally filesystem-first so the same
contract can be used by NeoEngine, NeoWealth, and future orgs from cron jobs,
agent lanes, and release gates.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

VERIFIER_NAME = "hermes-progress-verifier"
VERIFIER_VERSION = "0.1.0"
EVENT_SCHEMA = "org.evidence_event.v1"
CLOSEOUT_SCHEMA = "org.agent_closeout.v1"
PROOF_DEBT_SCHEMA = "org.proof_debt.v1"
ORG_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
PROMOTABLE_CLAIM_TYPES = {
    "PR_REBASED",
    "PR_OPENED",
    "CURRENT_HEAD_CHECKS_GREEN",
    "CURRENT_HEAD_SIDECAR_RECEIPT_BOUND",
    "POST_MERGE_VALIDATED",
}
FORBIDDEN_DELIVERY_CLAIMS = {"accepted", "landed", "deployed", "live", "release_ready", "closeout_ready"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in value).strip("-") or "item"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _validate_org(org: str) -> str:
    if not ORG_NAME_RE.fullmatch(org):
        raise ValueError(f"invalid org name: {org!r}")
    return org


def _critical_pr_numbers(policy: Mapping[str, Any]) -> set[Any]:
    return {item.get("number") for item in policy.get("critical_path", []) or [] if item.get("kind") == "pull_request"}


def _validate_pr_number(number: Any) -> int:
    if isinstance(number, bool):
        raise ValueError("invalid pull request number")
    if isinstance(number, int):
        if number <= 0:
            raise ValueError("invalid pull request number")
        return number
    if isinstance(number, str) and number.isdecimal() and int(number) > 0:
        return int(number)
    raise ValueError(f"invalid pull request number: {number!r}")


def _sidecar_evidence_bound(claim: Mapping[str, Any]) -> bool:
    evidence = claim.get("evidence", {})
    if not isinstance(evidence, Mapping):
        return False
    has_artifact = bool(evidence.get("sidecar_receipt") or evidence.get("evidence_url_or_path") or evidence.get("receipt_path"))
    return has_artifact and bool(evidence.get("diff_hash") or evidence.get("head_sha"))


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_agent_closeout_receipt(
    root: str | Path,
    *,
    org: str,
    repo: str,
    lane: str,
    agent: str,
    work_packet: str,
    subject: Mapping[str, Any],
    claims: list[Mapping[str, Any]],
    non_claims: list[str] | None = None,
    next_blocker: str | None = None,
    protected_boundary: bool = False,
    human_decision_required: bool = False,
) -> Path:
    """Write a schema-shaped candidate closeout receipt to an org inbox."""

    org = _validate_org(org)
    receipt = {
        "schema": CLOSEOUT_SCHEMA,
        "receipt_id": "rcpt_" + uuid.uuid4().hex,
        "created_at": _now(),
        "org": org,
        "lane": lane,
        "agent": agent,
        "work_packet": work_packet,
        "repo": repo,
        "subject": dict(subject),
        "claims": [dict(claim) for claim in claims],
        "non_claims": list(non_claims or []),
        "next_blocker": next_blocker,
        "protected_boundary": protected_boundary,
        "human_decision_required": human_decision_required,
    }
    pr = _validate_pr_number(subject.get("number") or subject.get("pr"))
    path = Path(root) / "inbox" / org / "candidate" / f"{receipt['created_at'].replace(':', '-')}-{_slug(lane)}-pr{pr}-{receipt['receipt_id']}.json"
    _write_json(path, receipt)
    return path


class OrgEvidenceFabric:
    """Promote agent candidate receipts into verified org evidence projections."""

    def __init__(self, root: str | Path, *, policies: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self.root = Path(root)
        self.policies = {_validate_org(org): dict(policy) for org, policy in (policies or {}).items()}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._debts: dict[str, list[dict[str, Any]]] = {}
        self._contradictions: dict[str, list[dict[str, Any]]] = {}
        self._results: dict[str, dict[str, int]] = {}

    def verify_all(self, *, live_state: Mapping[str, Any] | None = None) -> dict[str, dict[str, int]]:
        """Verify all org inboxes and refresh projections.

        ``live_state`` is injected by cron/tests; production wrappers can build it
        from GitHub, CI, deployment APIs, and local runtime receipts.
        """

        live_state = live_state or {}
        orgs = set(self.policies) | {_validate_org(p.parent.name) for p in (self.root / "inbox").glob("*/candidate")}
        for org in sorted(orgs):
            self._verify_org(org, live_state.get(org, {}))
            self._apply_policy_debt(org, live_state.get(org, {}))
            self._write_projections(org)
            self._write_verifier_run(org)
        self._write_all_org_projections(sorted(orgs))
        return deepcopy(self._results)

    def _empty_result(self) -> dict[str, int]:
        return {"promoted": 0, "rejected": 0, "stale": 0, "malformed": 0, "proof_debt": 0, "contradictions": 0}

    def _verify_org(self, org: str, org_live_state: Mapping[str, Any]) -> None:
        result = self._results.setdefault(org, self._empty_result())
        candidate_dir = self.root / "inbox" / org / "candidate"
        candidate_dir.mkdir(parents=True, exist_ok=True)
        for receipt_path in sorted(candidate_dir.glob("*.json")):
            receipt = _read_json(receipt_path)
            if not self._valid_receipt(receipt, org):
                result["malformed"] += 1
                self._move(receipt_path, self.root / "inbox" / org / "malformed" / receipt_path.name)
                continue
            subject = receipt.get("subject", {})
            live_pr = self._live_pr(org_live_state, subject.get("number"))
            if self._is_stale(subject, live_pr):
                result["stale"] += 1
                self._move(receipt_path, self.root / "stale" / org / receipt_path.name)
                self._add_debt(
                    org,
                    "CURRENT_HEAD_RECEIPT_REQUIRED",
                    subject={
                        "kind": "pull_request",
                        "number": subject.get("number"),
                        "old_head_sha": subject.get("head_sha"),
                        "current_head_sha": live_pr.get("head_sha"),
                    },
                    severity="P0",
                    required_artifact="current-head receipt bound to live PR head/base/diff",
                    eligible_lanes=self._eligible_lanes_for_pr(org, subject.get("number")),
                )
                continue
            promoted_any = False
            rejected_any = False
            for claim in receipt.get("claims", []):
                claim_type = claim.get("type")
                if self._claim_promotable(org, claim_type, live_pr, receipt):
                    event = self._append_event(org, receipt, claim, live_pr)
                    if event:
                        result["promoted"] += 1
                        promoted_any = True
                else:
                    rejected_any = True
                    result["rejected"] += 1
                    self._handle_rejection(org, receipt, claim, live_pr)
            destination_root = self.root / ("superseded" if promoted_any and not rejected_any else "rejected") / org
            self._move(receipt_path, destination_root / receipt_path.name)

    def _valid_receipt(self, receipt: Mapping[str, Any], org: str) -> bool:
        return (
            receipt.get("schema") == CLOSEOUT_SCHEMA
            and receipt.get("org") == org
            and isinstance(receipt.get("repo"), str)
            and isinstance(receipt.get("subject"), dict)
            and isinstance(receipt.get("claims"), list)
        )

    def _live_pr(self, org_live_state: Mapping[str, Any], number: Any) -> dict[str, Any]:
        prs = org_live_state.get("pull_requests", {}) if isinstance(org_live_state, Mapping) else {}
        return dict(prs.get(number) or prs.get(str(number)) or {}) if isinstance(prs, Mapping) else {}

    def _is_stale(self, subject: Mapping[str, Any], live_pr: Mapping[str, Any]) -> bool:
        return bool(live_pr.get("head_sha") and subject.get("head_sha") and live_pr.get("head_sha") != subject.get("head_sha"))

    def _claim_promotable(self, org: str, claim_type: str | None, live_pr: Mapping[str, Any], receipt: Mapping[str, Any]) -> bool:
        if not claim_type or not live_pr:
            return False
        if str(claim_type).lower() in FORBIDDEN_DELIVERY_CLAIMS or claim_type not in PROMOTABLE_CLAIM_TYPES:
            return False
        subject = receipt.get("subject", {})
        if subject.get("base_sha") and live_pr.get("base_sha") and subject.get("base_sha") != live_pr.get("base_sha"):
            return False
        if subject.get("head_sha") != live_pr.get("head_sha"):
            return False
        if claim_type == "CURRENT_HEAD_CHECKS_GREEN":
            return not self._required_check_debts(org, live_pr)
        if claim_type == "CURRENT_HEAD_SIDECAR_RECEIPT_BOUND":
            return _sidecar_evidence_bound(next((claim for claim in receipt.get("claims", []) if claim.get("type") == claim_type), {}))
        return True

    def _required_check_debts(self, org: str, live_pr: Mapping[str, Any]) -> list[tuple[str, str, str]]:
        checks = live_pr.get("checks", {}) if isinstance(live_pr.get("checks"), Mapping) else {}
        debts: list[tuple[str, str, str]] = []
        required_checks = self.policies.get(org, {}).get("required_checks")
        if not required_checks:
            debts.append(("REQUIRED_CHECK_POLICY_MISSING", "required_checks", "org policy does not define required checks"))
            return debts
        for check_name in required_checks:
            status = checks.get(check_name)
            normalized = str(status or "").upper()
            if not status:
                debts.append(("REQUIRED_CHECK_MISSING", check_name, "required check result is absent"))
            elif normalized == "CANCELLED":
                debts.append(("REQUIRED_CHECK_CANCELLED", check_name, "required check was cancelled"))
            elif normalized != "SUCCESS":
                debts.append(("REQUIRED_CHECK_NOT_SUCCESS", check_name, f"required check status is {status}"))
        return debts

    def _handle_rejection(self, org: str, receipt: Mapping[str, Any], claim: Mapping[str, Any], live_pr: Mapping[str, Any]) -> None:
        claim_type = str(claim.get("type") or "")
        subject = receipt.get("subject", {})
        if claim_type.lower() in FORBIDDEN_DELIVERY_CLAIMS or claim_type not in PROMOTABLE_CLAIM_TYPES:
            self._add_contradiction(
                org,
                subject={"kind": "pull_request", "number": subject.get("number"), "head_sha": subject.get("head_sha")},
                contradiction={"claim": claim_type or "missing", "conflicting_fact": "claim_type_not_independently_promotable"},
                required_resolution="submit a supported evidence claim or satisfy the delivery proof ladder before delivery-state claims",
            )
            self._add_debt(
                org,
                "FORBIDDEN_OR_UNKNOWN_CLAIM_REJECTED",
                subject={"kind": "pull_request", "number": subject.get("number"), "head_sha": subject.get("head_sha"), "claim_type": claim_type or "missing"},
                severity="P0" if claim_type.lower() in FORBIDDEN_DELIVERY_CLAIMS else "P2",
                required_artifact="verifier-supported claim type with non-claims; delivery-state claims require verified proof ladder",
                eligible_lanes=self._eligible_lanes_for_pr(org, subject.get("number")),
            )
        if not live_pr:
            self._add_debt(
                org,
                "LIVE_PR_STATE_MISSING",
                subject={"kind": "pull_request", "number": subject.get("number"), "head_sha": subject.get("head_sha")},
                severity="P0",
                required_artifact="live GitHub PR state bound to current head/base",
                eligible_lanes=self._eligible_lanes_for_pr(org, subject.get("number")),
            )
            return
        if subject.get("base_sha") and live_pr.get("base_sha") and subject.get("base_sha") != live_pr.get("base_sha"):
            self._add_debt(
                org,
                "CURRENT_BASE_RECEIPT_REQUIRED",
                subject={
                    "kind": "pull_request",
                    "number": subject.get("number"),
                    "head_sha": subject.get("head_sha"),
                    "receipt_base_sha": subject.get("base_sha"),
                    "current_base_sha": live_pr.get("base_sha"),
                },
                severity="P0",
                required_artifact="receipt rebound to current PR base SHA and diff",
                eligible_lanes=self._eligible_lanes_for_pr(org, subject.get("number")),
            )
        if claim_type == "CURRENT_HEAD_SIDECAR_RECEIPT_BOUND" and not _sidecar_evidence_bound(claim):
            self._add_debt(
                org,
                "SIDECAR_RECEIPT_ARTIFACT_MISSING",
                subject={"kind": "pull_request", "number": subject.get("number"), "head_sha": subject.get("head_sha")},
                severity="P0",
                required_artifact="sidecar artifact URL/path plus head or diff hash evidence",
                eligible_lanes=self._eligible_lanes_for_pr(org, subject.get("number")),
            )
        for debt_type, check_name, reason in self._required_check_debts(org, live_pr):
            self._add_contradiction(
                org,
                subject={"kind": "pull_request", "number": subject.get("number"), "head_sha": subject.get("head_sha")},
                contradiction={"claim": claim_type, "conflicting_fact": f"{debt_type.lower()}:{check_name}"},
                required_resolution=reason,
            )
            self._add_debt(
                org,
                debt_type,
                subject={"kind": "pull_request", "number": subject.get("number"), "head_sha": subject.get("head_sha"), "check": check_name},
                severity="P0",
                required_artifact=f"terminal SUCCESS for required check {check_name} or active-policy non-blocking evidence",
                eligible_lanes=self._eligible_lanes_for_pr(org, subject.get("number")),
            )
    def _append_event(self, org: str, receipt: Mapping[str, Any], claim: Mapping[str, Any], live_pr: Mapping[str, Any]) -> dict[str, Any] | None:
        subject = receipt.get("subject", {})
        idempotency_key = f"{org}:{receipt.get('repo')}:pr:{subject.get('number')}:head:{subject.get('head_sha')}:base:{subject.get('base_sha')}:{claim.get('type')}"
        if any(event.get("idempotency_key") == idempotency_key for event in self._load_events(org)):
            return None
        previous_hash = self._last_event_hash(org)
        event = {
            "schema": EVENT_SCHEMA,
            "event_id": "evt_" + uuid.uuid4().hex,
            "idempotency_key": idempotency_key,
            "event_type": claim.get("type"),
            "event_status": "verified",
            "org": org,
            "repo": receipt.get("repo"),
            "subject": {
                "kind": "pull_request",
                "number": subject.get("number"),
                "head_sha": subject.get("head_sha"),
                "base_sha": subject.get("base_sha"),
                "base_ref": live_pr.get("base_ref", "main"),
            },
            "bindings": {
                "roadmap_item_id": receipt.get("roadmap_item_id"),
                "blocker_id": receipt.get("blocker_id"),
                "capability_id": receipt.get("capability_id"),
                "environment": receipt.get("environment"),
                "agent_lane": receipt.get("lane"),
            },
            "claim": {"summary": claim.get("summary", claim.get("type")), "non_claims": receipt.get("non_claims", [])},
            "evidence": claim.get("evidence", {}),
            "verifier": {"name": VERIFIER_NAME, "version": VERIFIER_VERSION, "policy": self.policies.get(org, {}).get("schema"), "verified_at": _now()},
            "integrity": {"previous_event_hash": previous_hash},
        }
        event["integrity"]["event_hash"] = _sha256({k: v for k, v in event.items() if k != "integrity"} | {"previous_event_hash": previous_hash})
        event_path = self.root / "events" / org / "events.jsonl"
        event_path.parent.mkdir(parents=True, exist_ok=True)
        with event_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
        self._events.setdefault(org, []).append(event)
        return event

    def _last_event_hash(self, org: str) -> str | None:
        path = self.root / "events" / org / "events.jsonl"
        if not path.exists():
            return None
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            return None
        try:
            return json.loads(lines[-1])["integrity"]["event_hash"]
        except Exception:
            return None

    def _apply_policy_debt(self, org: str, org_live_state: Mapping[str, Any]) -> None:
        policy = self.policies.get(org, {})
        for item in policy.get("critical_path", []) or []:
            if item.get("kind") != "pull_request":
                continue
            number = item.get("number")
            live_pr = self._live_pr(org_live_state, number)
            if not live_pr:
                self._add_debt(
                    org,
                    "LIVE_PR_STATE_MISSING",
                    subject={"kind": "pull_request", "number": number},
                    severity="P0",
                    required_artifact="live GitHub PR state bound to current head/base",
                    eligible_lanes=item.get("eligible_lanes", []),
                )
                continue
            if str(live_pr.get("state", "OPEN")).upper() == "CLOSED":
                continue
            for debt_type, check_name, reason in self._required_check_debts(org, live_pr):
                self._add_debt(
                    org,
                    debt_type,
                    subject={"kind": "pull_request", "number": number, "head_sha": live_pr.get("head_sha"), "check": check_name},
                    severity="P0",
                    required_artifact=f"{reason}; provide terminal SUCCESS for required check {check_name} or active-policy non-blocking evidence",
                    eligible_lanes=item.get("eligible_lanes", []),
                )
            required_claims = set(item.get("required_claims", []))
            existing_types = {event.get("event_type") for event in self._load_events(org) if event.get("subject", {}).get("number") == number and event.get("subject", {}).get("head_sha") == live_pr.get("head_sha")}
            if "CURRENT_HEAD_SIDECAR_RECEIPT_BOUND" in required_claims and "CURRENT_HEAD_SIDECAR_RECEIPT_BOUND" not in existing_types:
                self._add_debt(
                    org,
                    "CURRENT_HEAD_SIDECAR_RECEIPT_MISSING",
                    subject={"kind": "pull_request", "number": number, "head_sha": live_pr.get("head_sha")},
                    severity="P0",
                    required_artifact="sidecar receipt bound to current PR head/base/diff",
                    eligible_lanes=item.get("eligible_lanes", []),
                )

    def _load_events(self, org: str) -> list[dict[str, Any]]:
        if org in self._events:
            return self._events[org]
        path = self.root / "events" / org / "events.jsonl"
        events: list[dict[str, Any]] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
        self._events[org] = events
        return events

    def _add_debt(self, org: str, debt_type: str, *, subject: Mapping[str, Any], severity: str, required_artifact: str, eligible_lanes: list[str] | None = None) -> None:
        item = {
            "schema": PROOF_DEBT_SCHEMA,
            "debt_id": "debt_" + uuid.uuid4().hex,
            "org": org,
            "debt_type": debt_type,
            "severity": severity,
            "subject": dict(subject),
            "required_artifact": required_artifact,
            "eligible_lanes": list(eligible_lanes or []),
            "invalid_lanes": ["CEO", "founder"],
            "protected_boundary": False,
            "status": "open",
            "created_at": _now(),
            "created_by": {"verifier": VERIFIER_NAME, "version": VERIFIER_VERSION},
        }
        self._debts.setdefault(org, [])
        key = _canonical_json({"debt_type": debt_type, "subject": item["subject"]})
        if key not in {_canonical_json({"debt_type": debt["debt_type"], "subject": debt["subject"]}) for debt in self._debts[org]}:
            self._debts[org].append(item)
            self._results.setdefault(org, self._empty_result())["proof_debt"] += 1

    def _add_contradiction(self, org: str, *, subject: Mapping[str, Any], contradiction: Mapping[str, Any], required_resolution: str) -> None:
        item = {
            "schema": EVENT_SCHEMA,
            "event_type": "CONTRADICTION_DETECTED",
            "event_status": "verified",
            "severity": "P0",
            "org": org,
            "subject": dict(subject),
            "contradiction": dict(contradiction),
            "required_resolution": required_resolution,
            "verified_at": _now(),
        }
        self._contradictions.setdefault(org, []).append(item)
        self._results.setdefault(org, self._empty_result())["contradictions"] += 1

    def _eligible_lanes_for_pr(self, org: str, number: Any) -> list[str]:
        for item in self.policies.get(org, {}).get("critical_path", []) or []:
            if item.get("kind") == "pull_request" and item.get("number") == number:
                return list(item.get("eligible_lanes", []))
        return []

    def _write_projections(self, org: str) -> None:
        events = self._load_events(org)
        debts = self._debts.get(org, [])
        contradictions = self._contradictions.get(org, [])
        projection_root = self.root / "projections" / org
        _write_json(
            projection_root / "product-progress-proof.json",
            {
                "schema": "org.product_progress_projection.v1",
                "org": org,
                "computed_at": _now(),
                "verified_movement": len(events),
                "candidate_progress": len(list((self.root / "inbox" / org / "candidate").glob("*.json"))),
                "proof_debt_open": len(debts),
                "contradictions": len(contradictions),
                "source_events": [event.get("event_id") for event in events],
                "not_merge_evidence": True,
            },
        )
        _write_json(projection_root / "proof-debt.json", {"schema": "org.proof_debt_projection.v1", "org": org, "computed_at": _now(), "open_debt": len(debts), "items": debts})
        _write_json(projection_root / "contradiction-ledger.json", {"schema": "org.contradiction_projection.v1", "org": org, "computed_at": _now(), "contradictions": len(contradictions), "items": contradictions})
        readiness_verified = any(event.get("event_type") == "RELEASE_READY_VERIFIED" for event in events)
        _write_json(projection_root / "release-readiness.json", {"schema": "org.release_readiness_projection.v1", "org": org, "computed_at": _now(), "ready": readiness_verified and not debts and not contradictions, "blocking_debt": [d["debt_type"] for d in debts], "forbidden_claims": sorted(FORBIDDEN_DELIVERY_CLAIMS) if debts or contradictions or not readiness_verified else []})

    def _write_all_org_projections(self, orgs: list[str]) -> None:
        projection_root = self.root / "projections" / "all-orgs"
        material_debt: list[dict[str, Any]] = []
        tickets: list[dict[str, Any]] = []
        totals = {"verified_movement": 0, "candidate_progress": 0, "proof_debt_open": 0, "contradictions": 0, "ready_orgs": 0}
        org_summaries: list[dict[str, Any]] = []
        for org in orgs:
            org_projection = _read_json(self.root / "projections" / org / "product-progress-proof.json")
            readiness = _read_json(self.root / "projections" / org / "release-readiness.json")
            debts = self._debts.get(org, [])
            contradictions = self._contradictions.get(org, [])
            totals["verified_movement"] += int(org_projection.get("verified_movement") or 0)
            totals["candidate_progress"] += int(org_projection.get("candidate_progress") or 0)
            totals["proof_debt_open"] += len(debts)
            totals["contradictions"] += len(contradictions)
            if readiness.get("ready") is True:
                totals["ready_orgs"] += 1
            org_summaries.append(
                {
                    "org": org,
                    "ready": readiness.get("ready") is True,
                    "verified_movement": int(org_projection.get("verified_movement") or 0),
                    "proof_debt_open": len(debts),
                    "contradictions": len(contradictions),
                }
            )
            for debt in debts:
                debt_summary = {
                    "org": org,
                    "debt_id": debt.get("debt_id"),
                    "debt_type": debt.get("debt_type"),
                    "severity": debt.get("severity"),
                    "subject": debt.get("subject", {}),
                    "required_artifact": debt.get("required_artifact"),
                    "eligible_lanes": debt.get("eligible_lanes", []),
                }
                material_debt.append(debt_summary)
                tickets.append(
                    {
                        "schema": "org.next_action_ticket.v1",
                        "ticket_id": "ticket_" + uuid.uuid4().hex,
                        "priority": debt.get("severity", "P2"),
                        "org": org,
                        "subject": debt.get("subject", {}),
                        "reason": debt.get("debt_type"),
                        "required_artifact": debt.get("required_artifact"),
                        "success_condition": f"{debt.get('debt_type')} resolved by verified evidence",
                        "eligible_lanes": debt.get("eligible_lanes", []),
                        "invalid_lanes": debt.get("invalid_lanes", ["CEO", "founder"]),
                        "protected_boundary": bool(debt.get("protected_boundary", False)),
                        "expires_when": ["subject moves", "subject closes", "required evidence promotes"],
                    }
                )
        _write_json(
            projection_root / "watchdog-summary.json",
            {
                "schema": "org.watchdog_summary_projection.v1",
                "computed_at": _now(),
                "orgs": orgs,
                "totals": totals,
                "org_summaries": org_summaries,
                "material_debt": material_debt,
                "non_claims": ["not_deployed", "not_accepted", "not_landed", "not_live"],
            },
        )
        _write_json(
            projection_root / "dispatch-tickets.json",
            {
                "schema": "org.dispatch_ticket_projection.v1",
                "computed_at": _now(),
                "ticket_count": len(tickets),
                "tickets": tickets,
            },
        )

    def _write_verifier_run(self, org: str) -> None:
        _write_json(
            self.root / "verifier-runs" / org / f"{_now().replace(':', '-')}.json",
            {"schema": "org.verifier_result.v1", "org": org, "verifier": VERIFIER_NAME, "verifier_version": VERIFIER_VERSION, "result": self._results.get(org, self._empty_result())},
        )

    def _move(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
