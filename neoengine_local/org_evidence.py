"""Policy-driven verified evidence fabric for org delivery control loops.

Agents write candidate closeout receipts into an inbox.  The verifier is the
only component that promotes those claims into canonical evidence events and
derived projections.  The module stays filesystem-first so NeoEngine,
NeoWealth, Everarc, and future policy-file orgs can share one fail-closed
operating-control substrate without hardcoded org names.
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
VERIFIER_VERSION = "0.2.0"
EVENT_SCHEMA = "org.evidence_event.v1"
CLOSEOUT_SCHEMA = "org.agent_closeout.v1"
PRODUCT_PROGRESS_SCHEMA = "org.product_progress_receipt.v1"
PROOF_DEBT_SCHEMA = "org.proof_debt.v1"
RELEASE_GATE_SCHEMA = "org.release_gate.v1"
NEXT_ACTION_TICKET_SCHEMA = "org.next_action_ticket.v1"
DISPATCH_ASSIGNMENT_SCHEMA = "org.dispatch_assignment.v1"
WATCHDOG_SUMMARY_SCHEMA = "org.watchdog_summary_projection.v1"
RELEASE_MEMORY_SCHEMA = "org.release_memory.v1"
POLICY_SCHEMA = "org.policy.v1"
POLICY_CHANGE_SCHEMA = "org.policy_change.v1"
ORG_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
PROMOTABLE_CLAIM_TYPES = {
    "PR_REBASED",
    "PR_OPENED",
    "CURRENT_HEAD_CHECKS_GREEN",
    "CURRENT_HEAD_SIDECAR_RECEIPT_BOUND",
    "POST_MERGE_VALIDATED",
    "MERGED_TO_MAIN",
    "ENABLED_CONFIGURATION_PROVEN",
    "DEPLOYED_RUNTIME_PROVEN",
    "OUTCOME_ACCEPTANCE_PROVEN",
}
FORBIDDEN_DELIVERY_CLAIMS = {"accepted", "landed", "deployed", "live", "release_ready", "closeout_ready"}
SCHEMA_REGISTRY = {
    EVENT_SCHEMA: {"required": ["schema", "event_id", "idempotency_key", "event_type", "event_status", "org", "subject", "integrity"]},
    CLOSEOUT_SCHEMA: {"required": ["schema", "receipt_id", "org", "repo", "subject", "claims"]},
    PRODUCT_PROGRESS_SCHEMA: {"required": ["schema", "org", "computed_at"]},
    PROOF_DEBT_SCHEMA: {"required": ["schema", "debt_id", "org", "debt_type", "subject", "required_artifact", "status"]},
    RELEASE_GATE_SCHEMA: {"required": ["schema", "gate", "org", "subject", "result"]},
    NEXT_ACTION_TICKET_SCHEMA: {"required": ["schema", "ticket_id", "org", "subject", "reason", "required_artifact"]},
    DISPATCH_ASSIGNMENT_SCHEMA: {"required": ["schema", "assignment_id", "ticket_id", "org", "lane", "status"]},
    WATCHDOG_SUMMARY_SCHEMA: {"required": ["schema", "computed_at", "orgs"]},
    RELEASE_MEMORY_SCHEMA: {"required": ["schema", "computed_at", "orgs"]},
    POLICY_SCHEMA: {"required": ["schema", "org", "critical_path", "required_checks"]},
    POLICY_CHANGE_SCHEMA: {"required": ["schema", "org", "old_policy", "new_policy"]},
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _week() -> str:
    d = datetime.now(timezone.utc)
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


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


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, items: list[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, sort_keys=True) + "\n")


def _validate_org(org: str) -> str:
    if not ORG_NAME_RE.fullmatch(str(org)):
        raise ValueError(f"invalid org name: {org!r}")
    return str(org)


def _validate_pr_number(number: Any) -> int:
    if isinstance(number, bool):
        raise ValueError("invalid pull request number")
    if isinstance(number, int) and number > 0:
        return number
    if isinstance(number, str) and number.isdecimal() and int(number) > 0:
        return int(number)
    raise ValueError(f"invalid pull request number: {number!r}")


def _schema_missing(data: Mapping[str, Any], schema: str) -> list[str]:
    if data.get("schema") != schema:
        return ["schema"]
    return [field for field in SCHEMA_REGISTRY[schema]["required"] if field not in data]


def _sidecar_evidence_bound(claim: Mapping[str, Any]) -> bool:
    evidence = claim.get("evidence", {})
    if not isinstance(evidence, Mapping):
        return False
    has_artifact = bool(evidence.get("sidecar_receipt") or evidence.get("evidence_url_or_path") or evidence.get("receipt_path"))
    return has_artifact and bool(evidence.get("diff_hash") or evidence.get("head_sha"))


def _receipt_key(receipt: Mapping[str, Any], claim: Mapping[str, Any], policy_hash: str) -> str:
    subject = receipt.get("subject", {})
    return ":".join(
        [
            str(receipt.get("org")),
            str(receipt.get("repo")),
            "pr",
            str(subject.get("number")),
            "head",
            str(subject.get("head_sha")),
            "base",
            str(subject.get("base_sha")),
            str(claim.get("type")),
            policy_hash,
        ]
    )


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
    pr = _validate_pr_number(subject.get("number") or subject.get("pr"))
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
    path = Path(root) / "inbox" / org / "candidate" / f"{receipt['created_at'].replace(':', '-')}-{_slug(lane)}-pr{pr}-{receipt['receipt_id']}.json"
    _write_json(path, receipt)
    return path


class OrgEvidenceFabric:
    """Promote agent receipts into verified all-org evidence projections."""

    def __init__(self, root: str | Path, *, policies: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self.root = Path(root)
        self.policies = {_validate_org(org): dict(policy) for org, policy in (policies or {}).items()}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._debts: dict[str, list[dict[str, Any]]] = {}
        self._contradictions: dict[str, list[dict[str, Any]]] = {}
        self._results: dict[str, dict[str, int]] = {}
        self._live_state: Mapping[str, Any] = {}
        self._dry_run = False
        self._promoted_ids: dict[str, list[str]] = {}
        self._rejected_receipts: dict[str, list[str]] = {}
        self._stale_receipts: dict[str, list[str]] = {}
        self._policy_errors: dict[str, list[str]] = {}

    def verify_all(self, *, live_state: Mapping[str, Any] | None = None, dry_run: bool = False) -> dict[str, dict[str, int]]:
        self._live_state = live_state or {}
        self._dry_run = dry_run
        orgs = set(self.policies) | {_validate_org(p.parent.name) for p in (self.root / "inbox").glob("*/candidate")}
        self._results = {}
        self._debts = {}
        self._contradictions = {}
        self._promoted_ids = {}
        self._rejected_receipts = {}
        self._stale_receipts = {}
        self._policy_errors = {}
        for org in sorted(orgs):
            self._load_events(org)
            self._validate_policy(org)
            self._verify_org(org, self._live_state.get(org, {}))
            self._apply_policy_debt(org, self._live_state.get(org, {}))
            if not dry_run:
                self._write_projections(org)
                self._write_verifier_run(org)
        if not dry_run:
            self._write_all_org_projections(sorted(orgs))
        return deepcopy(self._results)

    def replay_projection(self, org: str) -> dict[str, Any]:
        events = self._load_events(org)
        return {"org": org, "verified_movement": len(events), "source_ledger_hash": self._last_event_hash(org), "source_events": [e.get("event_id") for e in events]}

    def _empty_result(self) -> dict[str, int]:
        return {"promoted": 0, "rejected": 0, "stale": 0, "malformed": 0, "proof_debt": 0, "contradictions": 0}

    def _policy_hash(self, org: str) -> str:
        return _sha256(self.policies.get(org, {}))

    def _validate_policy(self, org: str) -> None:
        policy = self.policies.get(org, {})
        missing = _schema_missing(policy, POLICY_SCHEMA)
        if policy.get("org") != org:
            missing.append("org_matches_policy_key")
        if missing:
            self._policy_errors[org] = sorted(set(missing))
            self._add_debt(
                org,
                "POLICY_SCHEMA_INVALID",
                subject={"kind": "org_policy", "org": org, "missing": sorted(set(missing))},
                severity="P0",
                required_artifact="valid org.policy.v1 with schema/org/critical_path/required_checks",
                eligible_lanes=[],
            )

    def _valid_receipt(self, receipt: Mapping[str, Any], org: str) -> bool:
        return not _schema_missing(receipt, CLOSEOUT_SCHEMA) and receipt.get("org") == org and isinstance(receipt.get("subject"), dict) and isinstance(receipt.get("claims"), list)

    def _verify_org(self, org: str, org_live_state: Mapping[str, Any]) -> None:
        self._results.setdefault(org, self._empty_result())
        candidate_dir = self.root / "inbox" / org / "candidate"
        candidate_dir.mkdir(parents=True, exist_ok=True)
        for receipt_path in sorted(candidate_dir.glob("*.json")):
            receipt = _read_json(receipt_path)
            if not self._valid_receipt(receipt, org):
                self._results[org]["malformed"] += 1
                if not self._dry_run:
                    self._move(receipt_path, self.root / "inbox" / org / "malformed" / receipt_path.name)
                continue
            subject = receipt.get("subject", {})
            if receipt.get("protected_boundary") or receipt.get("human_decision_required"):
                self._add_debt(
                    org,
                    "PROTECTED_BOUNDARY_HIT",
                    subject={"kind": "pull_request", "number": subject.get("number"), "head_sha": subject.get("head_sha")},
                    severity="P0",
                    required_artifact="explicit protected-boundary human/operator decision before release gate can pass",
                    eligible_lanes=self._eligible_lanes_for_pr(org, subject.get("number")),
                )
            live_pr = self._live_pr(org_live_state, subject.get("number"))
            if self._is_stale(subject, live_pr):
                self._results[org]["stale"] += 1
                self._stale_receipts.setdefault(org, []).append(str(receipt.get("receipt_id")))
                self._add_debt(
                    org,
                    "CURRENT_HEAD_RECEIPT_REQUIRED",
                    subject={"kind": "pull_request", "number": subject.get("number"), "old_head_sha": subject.get("head_sha"), "current_head_sha": live_pr.get("head_sha")},
                    severity="P0",
                    required_artifact="current-head receipt bound to live PR head/base/diff",
                    eligible_lanes=self._eligible_lanes_for_pr(org, subject.get("number")),
                )
                if not self._dry_run:
                    self._move(receipt_path, self.root / "stale" / org / receipt_path.name)
                continue
            promoted_any = False
            rejected_any = False
            for claim in receipt.get("claims", []):
                claim_type = claim.get("type")
                if self._claim_promotable(org, claim_type, live_pr, receipt):
                    event = self._append_event(org, receipt, claim, live_pr)
                    if event:
                        self._results[org]["promoted"] += 1
                        self._promoted_ids.setdefault(org, []).append(str(event.get("event_id")))
                    promoted_any = True
                else:
                    rejected_any = True
                    self._results[org]["rejected"] += 1
                    self._rejected_receipts.setdefault(org, []).append(str(receipt.get("receipt_id")))
                    self._handle_rejection(org, receipt, claim, live_pr)
            if not self._dry_run:
                destination_root = self.root / ("superseded" if promoted_any and not rejected_any else "rejected") / org
                self._move(receipt_path, destination_root / receipt_path.name)

    def _live_pr(self, org_live_state: Mapping[str, Any], number: Any) -> dict[str, Any]:
        prs = org_live_state.get("pull_requests", {}) if isinstance(org_live_state, Mapping) else {}
        return dict(prs.get(number) or prs.get(str(number)) or {}) if isinstance(prs, Mapping) else {}

    def _is_stale(self, subject: Mapping[str, Any], live_pr: Mapping[str, Any]) -> bool:
        return bool(live_pr.get("head_sha") and subject.get("head_sha") and live_pr.get("head_sha") != subject.get("head_sha"))

    def _claim_promotable(self, org: str, claim_type: str | None, live_pr: Mapping[str, Any], receipt: Mapping[str, Any]) -> bool:
        if not claim_type or not live_pr or self._policy_errors.get(org):
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
            self._add_contradiction(org, subject={"kind": "pull_request", "number": subject.get("number"), "head_sha": subject.get("head_sha")}, contradiction={"claim": claim_type or "missing", "conflicting_fact": "claim_type_not_independently_promotable"}, required_resolution="submit a supported evidence claim or satisfy the delivery proof ladder before delivery-state claims")
            self._add_debt(org, "FORBIDDEN_OR_UNKNOWN_CLAIM_REJECTED", subject={"kind": "pull_request", "number": subject.get("number"), "head_sha": subject.get("head_sha"), "claim_type": claim_type or "missing"}, severity="P0" if claim_type.lower() in FORBIDDEN_DELIVERY_CLAIMS else "P2", required_artifact="verifier-supported claim type with non-claims; delivery-state claims require verified proof ladder", eligible_lanes=self._eligible_lanes_for_pr(org, subject.get("number")))
        if not live_pr:
            self._add_debt(org, "LIVE_PR_STATE_MISSING", subject={"kind": "pull_request", "number": subject.get("number"), "head_sha": subject.get("head_sha")}, severity="P0", required_artifact="live GitHub PR state bound to current head/base", eligible_lanes=self._eligible_lanes_for_pr(org, subject.get("number")))
            return
        if subject.get("base_sha") and live_pr.get("base_sha") and subject.get("base_sha") != live_pr.get("base_sha"):
            self._add_debt(org, "CURRENT_BASE_RECEIPT_REQUIRED", subject={"kind": "pull_request", "number": subject.get("number"), "head_sha": subject.get("head_sha"), "receipt_base_sha": subject.get("base_sha"), "current_base_sha": live_pr.get("base_sha")}, severity="P0", required_artifact="receipt rebound to current PR base SHA and diff", eligible_lanes=self._eligible_lanes_for_pr(org, subject.get("number")))
        if claim_type == "CURRENT_HEAD_SIDECAR_RECEIPT_BOUND" and not _sidecar_evidence_bound(claim):
            self._add_debt(org, "SIDECAR_RECEIPT_ARTIFACT_MISSING", subject={"kind": "pull_request", "number": subject.get("number"), "head_sha": subject.get("head_sha")}, severity="P0", required_artifact="sidecar artifact URL/path plus head or diff hash evidence", eligible_lanes=self._eligible_lanes_for_pr(org, subject.get("number")))
        for debt_type, check_name, reason in self._required_check_debts(org, live_pr):
            self._add_contradiction(org, subject={"kind": "pull_request", "number": subject.get("number"), "head_sha": subject.get("head_sha")}, contradiction={"claim": claim_type, "conflicting_fact": f"{debt_type.lower()}:{check_name}"}, required_resolution=reason)
            self._add_debt(org, debt_type, subject={"kind": "pull_request", "number": subject.get("number"), "head_sha": subject.get("head_sha"), "check": check_name}, severity="P0", required_artifact=f"terminal SUCCESS for required check {check_name} or active-policy non-blocking evidence", eligible_lanes=self._eligible_lanes_for_pr(org, subject.get("number")))

    def _append_event(self, org: str, receipt: Mapping[str, Any], claim: Mapping[str, Any], live_pr: Mapping[str, Any]) -> dict[str, Any] | None:
        idempotency_key = _receipt_key(receipt, claim, self._policy_hash(org))
        if any(event.get("idempotency_key") == idempotency_key for event in self._load_events(org)):
            return None
        subject = receipt.get("subject", {})
        previous_hash = self._last_event_hash(org)
        event = {
            "schema": EVENT_SCHEMA,
            "event_id": "evt_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:24],
            "idempotency_key": idempotency_key,
            "event_type": claim.get("type"),
            "event_status": "verified",
            "org": org,
            "repo": receipt.get("repo"),
            "subject": {"kind": "pull_request", "number": subject.get("number"), "head_sha": subject.get("head_sha"), "base_sha": subject.get("base_sha"), "base_ref": live_pr.get("base_ref", "main")},
            "bindings": {"roadmap_item_id": receipt.get("roadmap_item_id") or claim.get("roadmap_item_id"), "blocker_id": receipt.get("blocker_id") or claim.get("blocker_id"), "capability_id": receipt.get("capability_id") or claim.get("capability_id"), "environment": receipt.get("environment") or claim.get("environment"), "agent_lane": receipt.get("lane"), "work_packet": receipt.get("work_packet"), "agent": receipt.get("agent")},
            "claim": {"summary": claim.get("summary", claim.get("type")), "non_claims": receipt.get("non_claims", [])},
            "evidence": claim.get("evidence", {}),
            "verifier": {"name": VERIFIER_NAME, "version": VERIFIER_VERSION, "policy": self.policies.get(org, {}).get("schema"), "policy_hash": self._policy_hash(org), "verified_at": _now()},
            "integrity": {"previous_event_hash": previous_hash},
        }
        event["integrity"]["event_hash"] = _sha256({k: v for k, v in event.items() if k != "integrity"} | {"previous_event_hash": previous_hash})
        if not self._dry_run:
            event_path = self.root / "events" / org / "events.jsonl"
            event_path.parent.mkdir(parents=True, exist_ok=True)
            with event_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, sort_keys=True) + "\n")
            self._events.setdefault(org, []).append(event)
        return event

    def _last_event_hash(self, org: str) -> str | None:
        events = self._load_events(org)
        return events[-1].get("integrity", {}).get("event_hash") if events else None

    def _load_events(self, org: str) -> list[dict[str, Any]]:
        if org in self._events:
            return self._events[org]
        path = self.root / "events" / org / "events.jsonl"
        events: list[dict[str, Any]] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        events.append(item)
                except Exception:
                    continue
        self._events[org] = events
        return events

    def _apply_policy_debt(self, org: str, org_live_state: Mapping[str, Any]) -> None:
        policy = self.policies.get(org, {})
        if self._policy_errors.get(org):
            return
        for item in policy.get("critical_path", []) or []:
            if item.get("kind") != "pull_request":
                continue
            number = item.get("number")
            live_pr = self._live_pr(org_live_state, number)
            if not live_pr:
                self._add_debt(org, "LIVE_PR_STATE_MISSING", subject={"kind": "pull_request", "number": number}, severity="P0", required_artifact="live GitHub PR state bound to current head/base", eligible_lanes=item.get("eligible_lanes", []))
                continue
            for debt_type, check_name, reason in self._required_check_debts(org, live_pr):
                self._add_debt(org, debt_type, subject={"kind": "pull_request", "number": number, "head_sha": live_pr.get("head_sha"), "check": check_name}, severity="P0", required_artifact=f"{reason}; provide terminal SUCCESS for required check {check_name} or active-policy non-blocking evidence", eligible_lanes=item.get("eligible_lanes", []))
            existing_types = {event.get("event_type") for event in self._load_events(org) if event.get("subject", {}).get("number") == number and event.get("subject", {}).get("head_sha") == live_pr.get("head_sha")}
            for claim in item.get("required_claims", []):
                if claim not in existing_types:
                    debt_type = "CURRENT_HEAD_SIDECAR_RECEIPT_MISSING" if claim == "CURRENT_HEAD_SIDECAR_RECEIPT_BOUND" else f"{claim}_MISSING"
                    self._add_debt(org, debt_type, subject={"kind": "pull_request", "number": number, "head_sha": live_pr.get("head_sha")}, severity="P0", required_artifact="sidecar receipt bound to current PR head/base/diff" if claim == "CURRENT_HEAD_SIDECAR_RECEIPT_BOUND" else f"verified {claim} evidence", eligible_lanes=item.get("eligible_lanes", []))
            if str(live_pr.get("state", "")).upper() in {"MERGED", "CLOSED"} and item.get("number") == number and "POST_MERGE_VALIDATED" not in existing_types:
                self._add_debt(org, "POST_MERGE_VALIDATION_MISSING", subject={"kind": "pull_request", "number": number, "head_sha": live_pr.get("head_sha"), "merge_commit_sha": live_pr.get("merge_commit_sha")}, severity="P0", required_artifact="post-merge validation receipt from canonical main/merge commit", eligible_lanes=item.get("eligible_lanes", []))

    def _add_debt(self, org: str, debt_type: str, *, subject: Mapping[str, Any], severity: str, required_artifact: str, eligible_lanes: list[str] | None = None) -> None:
        item = {"schema": PROOF_DEBT_SCHEMA, "debt_id": "debt_" + hashlib.sha256(_canonical_json({"org": org, "type": debt_type, "subject": subject}).encode()).hexdigest()[:24], "org": org, "debt_type": debt_type, "severity": severity, "subject": dict(subject), "required_artifact": required_artifact, "eligible_lanes": list(eligible_lanes or []), "invalid_lanes": list(self.policies.get(org, {}).get("invalid_lanes", ["CEO", "founder"])), "protected_boundary": debt_type in {"REQUIRED_CHECK_BYPASS_ATTEMPT", "PROTECTED_BOUNDARY_HIT"}, "status": "open", "created_at": _now(), "created_by": {"verifier": VERIFIER_NAME, "version": VERIFIER_VERSION}}
        self._debts.setdefault(org, [])
        key = _canonical_json({"debt_type": debt_type, "subject": item["subject"]})
        if key not in {_canonical_json({"debt_type": debt["debt_type"], "subject": debt["subject"]}) for debt in self._debts[org]}:
            self._debts[org].append(item)
            self._results.setdefault(org, self._empty_result())["proof_debt"] += 1

    def _add_contradiction(self, org: str, *, subject: Mapping[str, Any], contradiction: Mapping[str, Any], required_resolution: str) -> None:
        key = f"{org}:contradiction:{_sha256({'subject': subject, 'contradiction': contradiction})}"
        previous_hash = self._last_event_hash(org)
        item = {"schema": EVENT_SCHEMA, "event_id": "ctr_" + hashlib.sha256(key.encode()).hexdigest()[:24], "idempotency_key": key, "event_type": "CONTRADICTION_DETECTED", "event_status": "verified", "severity": "P0", "org": org, "subject": dict(subject), "contradiction": dict(contradiction), "required_resolution": required_resolution, "verified_at": _now(), "integrity": {"previous_event_hash": previous_hash}}
        item["integrity"]["event_hash"] = _sha256({k: v for k, v in item.items() if k != "integrity"} | {"previous_event_hash": previous_hash})
        self._contradictions.setdefault(org, [])
        if item["event_id"] not in {c.get("event_id") for c in self._contradictions[org]}:
            self._contradictions[org].append(item)
            self._results.setdefault(org, self._empty_result())["contradictions"] += 1

    def _eligible_lanes_for_pr(self, org: str, number: Any) -> list[str]:
        for item in self.policies.get(org, {}).get("critical_path", []) or []:
            if item.get("kind") == "pull_request" and item.get("number") == number:
                return list(item.get("eligible_lanes", []))
        return []

    def _critical_subjects(self, org: str) -> list[dict[str, Any]]:
        subjects = []
        for item in self.policies.get(org, {}).get("critical_path", []) or []:
            if item.get("kind") == "pull_request":
                live_pr = self._live_pr(self._live_state.get(org, {}), item.get("number"))
                subjects.append({"kind": "pull_request", "number": item.get("number"), "head_sha": live_pr.get("head_sha"), "base_sha": live_pr.get("base_sha")})
        if not subjects:
            subjects.append({"kind": "org_policy", "org": org})
        return subjects

    def _evaluate_gate(self, org: str, subject: Mapping[str, Any], gate_name: str = "critical_pr_closeout_ready") -> dict[str, Any]:
        events = self._load_events(org)
        debts = [d for d in self._debts.get(org, []) if self._debt_matches_subject(d, subject)]
        contradictions = [c for c in self._contradictions.get(org, []) if c.get("subject", {}).get("number") == subject.get("number") or subject.get("kind") == "org_policy"]
        event_types = {e.get("event_type") for e in events if e.get("subject", {}).get("number") == subject.get("number") and (not subject.get("head_sha") or e.get("subject", {}).get("head_sha") == subject.get("head_sha"))}
        required = ["current-head receipt", "current-head CI/check evidence", "semantic non-claims", "blocker/roadmap/capability mapping", "protected-boundary declaration", "post-merge validation plan"]
        missing = [d.get("debt_type") for d in debts]
        if "CURRENT_HEAD_CHECKS_GREEN" not in event_types:
            missing.append("CURRENT_HEAD_CHECKS_GREEN")
        if any(d.get("debt_type") == "CURRENT_HEAD_SIDECAR_RECEIPT_MISSING" for d in debts):
            missing.append("CURRENT_HEAD_SIDECAR_RECEIPT_BOUND")
        result = "pass" if not missing and not contradictions and not self._policy_errors.get(org) else "fail"
        source_hash = self._last_event_hash(org) or _sha256({"org": org, "empty_ledger": True})
        gate = {"schema": RELEASE_GATE_SCHEMA, "gate": gate_name, "org": org, "subject": dict(subject), "result": result, "blocking_reasons": sorted(set(missing + ["contradiction" for _ in contradictions] + self._policy_errors.get(org, []))), "non_blocking_warnings": [], "required_evidence": required, "present_evidence": sorted(event_types), "missing_evidence": sorted(set(missing)), "allowed_claims": sorted(event_types), "forbidden_claims": [] if result == "pass" else sorted(FORBIDDEN_DELIVERY_CLAIMS), "source_event_ids": [e.get("event_id") for e in events], "source_ledger_hash": source_hash, "policy_version_hash": self._policy_hash(org), "next_required_artifact": debts[0].get("required_artifact") if debts else ("verified release gate evidence" if result != "pass" else None), "explainability": {"source_event_ids": [e.get("event_id") for e in events], "subject_binding": dict(subject), "verifier_run": VERIFIER_NAME, "policy_version_hash": self._policy_hash(org), "proof_ladder_rung": "blocked" if result != "pass" else "critical_pr_closeout_ready", "evidence_artifacts": [e.get("evidence") for e in events], "non_claims": ["not_deployed", "not_live", "not_accepted", "not_landed"], "staleness_status": "current" if result == "pass" else "blocked", "source_ledger_hash": source_hash}}
        gate["projection_hash"] = _sha256(gate)
        return gate

    def _debt_matches_subject(self, debt: Mapping[str, Any], subject: Mapping[str, Any]) -> bool:
        ds = debt.get("subject", {})
        return subject.get("kind") == "org_policy" or ds.get("number") == subject.get("number") or ds.get("kind") == "org_policy"

    def _build_tickets(self, orgs: list[str]) -> list[dict[str, Any]]:
        tickets = []
        for org in orgs:
            for debt in self._debts.get(org, []):
                tid = "ticket_" + hashlib.sha256(str(debt.get("debt_id")).encode()).hexdigest()[:24]
                tickets.append({"schema": NEXT_ACTION_TICKET_SCHEMA, "ticket_id": tid, "priority": debt.get("severity", "P2"), "org": org, "subject": debt.get("subject", {}), "reason": debt.get("debt_type"), "required_artifact": debt.get("required_artifact"), "success_condition": f"{debt.get('debt_type')} resolved by verified evidence", "eligible_lanes": debt.get("eligible_lanes", []), "invalid_lanes": debt.get("invalid_lanes", ["CEO", "founder"]), "protected_boundary": bool(debt.get("protected_boundary", False)), "expires_when": ["subject moves", "subject closes", "required evidence promotes", "policy changes invalidate it"], "status": "unassigned"})
        return tickets

    def _build_graph(self, org: str | None, orgs: list[str]) -> dict[str, Any]:
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        selected_orgs = [org] if org else orgs
        for o in selected_orgs:
            nodes[f"org:{o}"] = {"id": f"org:{o}", "kind": "org", "org": o}
            for event in self._load_events(o):
                sid = f"receipt:{event.get('event_id')}"
                pid = f"pr:{o}:{event.get('subject', {}).get('number')}"
                cid = f"commit:{event.get('subject', {}).get('head_sha')}"
                nodes[sid] = {"id": sid, "kind": "receipt", "status": event.get("event_status")}
                nodes[pid] = {"id": pid, "kind": "pull_request", "number": event.get("subject", {}).get("number")}
                nodes[cid] = {"id": cid, "kind": "commit", "sha": event.get("subject", {}).get("head_sha")}
                lane = event.get("bindings", {}).get("agent_lane")
                if lane:
                    nodes[f"lane:{lane}"] = {"id": f"lane:{lane}", "kind": "agent_lane"}
                    edges.append({"from": f"lane:{lane}", "to": sid, "type": "agent_lane_produced_receipt"})
                edges += [{"from": sid, "to": pid, "type": "receipt_claims_event"}, {"from": sid, "to": cid, "type": "check_suite_proves_commit"}]
                for key, kind, edge_type in [("roadmap_item_id", "roadmap_item", "pr_implements_roadmap_item"), ("blocker_id", "blocker", "pr_closes_blocker"), ("capability_id", "capability", "pr_changes_capability"), ("environment", "environment", "deployment_proves_environment")]:
                    value = event.get("bindings", {}).get(key)
                    if value:
                        nid = f"{kind}:{value}"
                        nodes[nid] = {"id": nid, "kind": kind}
                        edges.append({"from": pid, "to": nid, "type": edge_type})
            for debt in self._debts.get(o, []):
                did = f"proof_debt:{debt.get('debt_id')}"
                gid = f"release_gate:{o}:critical_pr_closeout_ready"
                nodes[did] = {"id": did, "kind": "proof_debt", "debt_type": debt.get("debt_type")}
                nodes[gid] = {"id": gid, "kind": "release_gate"}
                edges.append({"from": did, "to": gid, "type": "proof_debt_blocks_release_gate"})
            for c in self._contradictions.get(o, []):
                cid = f"contradiction:{c.get('event_id')}"
                nodes[cid] = {"id": cid, "kind": "contradiction"}
                edges.append({"from": cid, "to": f"org:{o}", "type": "contradiction_invalidates_readiness"})
            for item in self.policies.get(o, {}).get("critical_path", []) or []:
                if item.get("kind") == "pull_request":
                    nodes[f"pr:{o}:{item.get('number')}"] = {"id": f"pr:{o}:{item.get('number')}", "kind": "pull_request", "number": item.get("number")}
        return {"schema": "org.progress_graph.v1", "computed_at": _now(), "org": org or "all-orgs", "nodes": list(nodes.values()), "edges": edges}

    def _write_indexes(self, projection_root: Path, org: str, events: list[dict[str, Any]], gates: list[dict[str, Any]]) -> None:
        index_root = projection_root / "indexes"
        debts = self._debts.get(org, [])
        idx: dict[str, dict[str, Any]] = {
            "by-sha.json": {},
            "by-blocker.json": {},
            "by-lane.json": {},
            "by-agent.json": {},
            "by-roadmap-item.json": {},
            "by-capability.json": {},
            "by-proof-type.json": {},
            "by-environment.json": {},
            "by-critical-path.json": {},
            "by-release-gate.json": {},
        }
        for event in events:
            subj = event.get("subject", {})
            bindings = event.get("bindings", {})
            self._idx_add(idx["by-sha.json"], subj.get("head_sha"), event.get("event_id"))
            self._idx_add(idx["by-lane.json"], bindings.get("agent_lane"), event.get("event_id"))
            self._idx_add(idx["by-agent.json"], bindings.get("agent"), event.get("event_id"))
            self._idx_add(idx["by-blocker.json"], bindings.get("blocker_id"), event.get("event_id"))
            self._idx_add(idx["by-roadmap-item.json"], bindings.get("roadmap_item_id"), event.get("event_id"))
            self._idx_add(idx["by-capability.json"], bindings.get("capability_id"), event.get("event_id"))
            self._idx_add(idx["by-proof-type.json"], event.get("event_type"), event.get("event_id"))
            self._idx_add(idx["by-environment.json"], bindings.get("environment"), event.get("event_id"))
        for item in self.policies.get(org, {}).get("critical_path", []) or []:
            if item.get("kind") == "pull_request":
                idx["by-critical-path.json"][f"pull_request:{item.get('number')}"] = {"org": org, "debt": [d.get("debt_id") for d in debts if d.get("subject", {}).get("number") == item.get("number")]}
        for gate in gates:
            idx["by-release-gate.json"][gate["gate"]] = {"result": gate["result"], "blocking_reasons": gate["blocking_reasons"]}
        for name, data in idx.items():
            _write_json(index_root / name, data)

    def _idx_add(self, target: dict[str, Any], key: Any, value: Any) -> None:
        if key:
            target.setdefault(str(key), []).append(value)

    def _write_projections(self, org: str) -> None:
        events = self._load_events(org)
        debts = self._debts.get(org, [])
        contradictions = self._contradictions.get(org, [])
        projection_root = self.root / "projections" / org
        ledger_hash = self._last_event_hash(org) or _sha256({"org": org, "empty_ledger": True})
        progress = {"schema": "org.product_progress_projection.v1", "org": org, "computed_at": _now(), "verified_movement": len(events), "candidate_progress": len(list((self.root / "inbox" / org / "candidate").glob("*.json"))), "proof_debt_open": len(debts), "contradictions": len(contradictions), "source_events": [event.get("event_id") for event in events], "source_ledger_hash": ledger_hash, "not_merge_evidence": True}
        progress["projection_hash"] = _sha256(progress)
        _write_json(projection_root / "product-progress-proof.json", progress)
        _write_json(projection_root / "proof-debt.json", {"schema": "org.proof_debt_projection.v1", "org": org, "computed_at": _now(), "open_debt": len(debts), "items": debts, "source_ledger_hash": ledger_hash})
        _write_json(projection_root / "contradiction-ledger.json", {"schema": "org.contradiction_projection.v1", "org": org, "computed_at": _now(), "contradictions": len(contradictions), "items": contradictions, "source_ledger_hash": ledger_hash})
        gates = [self._evaluate_gate(org, subject) for subject in self._critical_subjects(org)]
        _write_json(projection_root / "release-gates.json", {"schema": "org.release_gate_projection.v1", "org": org, "computed_at": _now(), "gates": gates, "source_ledger_hash": ledger_hash})
        readiness_verified = any(g["result"] == "pass" for g in gates)
        _write_json(projection_root / "release-readiness.json", {"schema": "org.release_readiness_projection.v1", "org": org, "computed_at": _now(), "ready": readiness_verified and not debts and not contradictions, "blocking_debt": [d["debt_type"] for d in debts], "forbidden_claims": sorted(FORBIDDEN_DELIVERY_CLAIMS) if debts or contradictions or not readiness_verified else [], "source_ledger_hash": ledger_hash, "next_required_artifact": gates[0].get("next_required_artifact") if gates else None})
        graph = self._build_graph(org, [org])
        _write_json(projection_root / "progress-graph.json", graph)
        self._write_indexes(projection_root, org, events, gates)
        dispatch_state = self._dispatch_state([org])
        _write_json(projection_root / "dispatch-state.json", dispatch_state)
        _write_json(projection_root / "protected-boundaries.json", {"schema": "org.protected_boundary_projection.v1", "org": org, "computed_at": _now(), "items": [d for d in debts if d.get("protected_boundary")], "source_ledger_hash": ledger_hash})

    def _write_all_org_projections(self, orgs: list[str]) -> None:
        projection_root = self.root / "projections" / "all-orgs"
        tickets = self._build_tickets(orgs)
        material_debt: list[dict[str, Any]] = []
        gates: list[dict[str, Any]] = []
        aftercare: list[dict[str, Any]] = []
        totals = {"verified_movement": 0, "candidate_progress": 0, "proof_debt_open": 0, "contradictions": 0, "ready_orgs": 0}
        org_summaries: list[dict[str, Any]] = []
        ledger_hashes = {org: self._last_event_hash(org) or _sha256({"org": org, "empty_ledger": True}) for org in orgs}
        for org in orgs:
            org_projection = _read_json(self.root / "projections" / org / "product-progress-proof.json")
            readiness = _read_json(self.root / "projections" / org / "release-readiness.json")
            org_gates = _read_json(self.root / "projections" / org / "release-gates.json").get("gates", [])
            debts = self._debts.get(org, [])
            contradictions = self._contradictions.get(org, [])
            totals["verified_movement"] += int(org_projection.get("verified_movement") or 0)
            totals["candidate_progress"] += int(org_projection.get("candidate_progress") or 0)
            totals["proof_debt_open"] += len(debts)
            totals["contradictions"] += len(contradictions)
            if readiness.get("ready") is True:
                totals["ready_orgs"] += 1
            org_summaries.append({"org": org, "ready": readiness.get("ready") is True, "verified_movement": int(org_projection.get("verified_movement") or 0), "proof_debt_open": len(debts), "contradictions": len(contradictions), "next_required_artifact": readiness.get("next_required_artifact")})
            gates.extend(org_gates)
            for debt in debts:
                material_debt.append({"org": org, "debt_id": debt.get("debt_id"), "debt_type": debt.get("debt_type"), "severity": debt.get("severity"), "subject": debt.get("subject", {}), "required_artifact": debt.get("required_artifact"), "eligible_lanes": debt.get("eligible_lanes", [])})
                if debt.get("debt_type") == "POST_MERGE_VALIDATION_MISSING":
                    aftercare.append({"org": org, "subject": debt.get("subject"), "required_artifact": debt.get("required_artifact"), "debt_id": debt.get("debt_id")})
        summary = {"schema": WATCHDOG_SUMMARY_SCHEMA, "computed_at": _now(), "orgs": orgs, "totals": totals, "org_summaries": org_summaries, "material_debt": material_debt, "non_claims": ["not_deployed", "not_accepted", "not_landed", "not_live"], "source_ledger_hashes": ledger_hashes}
        summary["projection_hash"] = _sha256(summary)
        _write_json(projection_root / "watchdog-summary.json", summary)
        _write_json(projection_root / "dispatch-tickets.json", {"schema": "org.dispatch_ticket_projection.v1", "computed_at": _now(), "ticket_count": len(tickets), "tickets": tickets, "source_ledger_hashes": ledger_hashes})
        _write_json(projection_root / "release-gates.json", {"schema": "org.release_gate_projection.v1", "computed_at": _now(), "gates": gates, "source_ledger_hashes": ledger_hashes})
        _write_json(projection_root / "dispatch-state.json", self._dispatch_state(orgs))
        _write_json(projection_root / "progress-graph.json", self._build_graph(None, orgs))
        self._write_all_org_indexes(orgs)
        _write_json(projection_root / "watchdog-views.json", self._watchdog_views(orgs, summary, tickets, gates))
        _write_json(projection_root / "material-notifications.json", self._material_notifications(orgs, material_debt, gates, aftercare))
        _write_json(projection_root / "anti-theater-scorecard.json", self._scorecard(orgs))
        _write_json(projection_root / "merge-aftercare.json", {"schema": "org.merge_aftercare_projection.v1", "computed_at": _now(), "items": aftercare, "source_ledger_hashes": ledger_hashes})
        protected = [d for org in orgs for d in self._debts.get(org, []) if d.get("protected_boundary")]
        _write_json(projection_root / "protected-boundaries.json", {"schema": "org.protected_boundary_projection.v1", "computed_at": _now(), "items": protected, "source_ledger_hashes": ledger_hashes})
        memory = self._release_memory(orgs)
        _write_json(projection_root / "release-memory.json", memory)
        _write_json(self.root / "memory" / "all-orgs" / "daily" / f"{_today()}.json", memory)
        _write_json(self.root / "memory" / "all-orgs" / "weekly" / f"{_week()}.json", memory)
        for org in orgs:
            org_memory = self._release_memory([org])
            _write_json(self.root / "memory" / org / "daily" / f"{_today()}.json", org_memory)
            _write_json(self.root / "memory" / org / "weekly" / f"{_week()}.json", org_memory)

    def _write_all_org_indexes(self, orgs: list[str]) -> None:
        root = self.root / "projections" / "all-orgs" / "indexes"
        by_org = {org: {"events": len(self._load_events(org)), "proof_debt": len(self._debts.get(org, []))} for org in orgs}
        by_critical = {}
        by_proof = {}
        for org in orgs:
            by_critical[org] = list(_read_json(self.root / "projections" / org / "indexes" / "by-critical-path.json").keys())
            for event in self._load_events(org):
                by_proof.setdefault(str(event.get("event_type")), []).append({"org": org, "event_id": event.get("event_id")})
        _write_json(root / "by-org.json", by_org)
        _write_json(root / "by-critical-path.json", by_critical)
        _write_json(root / "by-proof-type.json", by_proof)

    def _dispatch_state(self, orgs: list[str]) -> dict[str, Any]:
        assigned = list((self.root / "dispatch" / "assigned").glob("*/*/*.json")) + list((self.root / "dispatch" / "assigned").glob("*/*.json"))
        closed = list((self.root / "dispatch" / "closed").glob("*/*.json"))
        rejected = list((self.root / "dispatch" / "rejected").glob("*/*.json"))
        return {"schema": "org.dispatch_state_projection.v1", "computed_at": _now(), "orgs": orgs, "unassigned": sum(len(self._debts.get(org, [])) for org in orgs), "assigned": len(assigned), "closed": len(closed), "rejected": len(rejected), "non_claims": ["assignment_does_not_imply_readiness"]}

    def _watchdog_views(self, orgs: list[str], summary: Mapping[str, Any], tickets: list[dict[str, Any]], gates: list[dict[str, Any]]) -> dict[str, Any]:
        blocked = [g for g in gates if g.get("result") != "pass"]
        view_data = {
            "founder": {"protected_decisions_required": [d for org in orgs for d in self._debts.get(org, []) if d.get("protected_boundary")], "material_movement": summary.get("org_summaries", []), "critical_risk": [g.get("blocking_reasons") for g in blocked], "next_owner": "conductor/eligible lane from dispatch ticket"},
            "ceo": {"delivery_truth": summary.get("totals"), "activity_vs_verified_movement": self._scorecard(orgs).get("orgs"), "accepted_outcome_count": 0, "business_product_non_claims": ["not_live", "not_accepted", "not_landed"]},
            "cto": {"current_head_state": summary.get("org_summaries"), "proof_debt": summary.get("material_debt"), "gate_state": gates, "next_technical_artifact": [g.get("next_required_artifact") for g in blocked]},
            "conductor": {"tickets": tickets, "owners": [t.get("eligible_lanes") for t in tickets], "stale_receipts": self._stale_receipts, "blocked_lanes": [t.get("invalid_lanes") for t in tickets], "dispatch_health": self._dispatch_state(orgs)},
            "agent": {"assigned_ticket": None, "required_artifact": [t.get("required_artifact") for t in tickets], "forbidden_claims": sorted(FORBIDDEN_DELIVERY_CLAIMS), "verifier_failure_reasons": [g.get("blocking_reasons") for g in blocked]},
        }
        return {"schema": "org.watchdog_role_views.v1", "computed_at": _now(), "views": view_data, "source": "verified projections only"}

    def _material_notifications(self, orgs: list[str], material_debt: list[dict[str, Any]], gates: list[dict[str, Any]], aftercare: list[dict[str, Any]]) -> dict[str, Any]:
        events = []
        for debt in material_debt:
            key = _sha256({"kind": "proof_debt", "debt": debt})
            events.append({"idempotency_key": key, "org": debt.get("org"), "event_type": "PROOF_DEBT_INCREASED", "summary": f"Proof debt increased: {debt.get('debt_type')}", "severity": debt.get("severity", "P2")})
        for org in orgs:
            for event in self._load_events(org):
                events.append({"idempotency_key": event.get("idempotency_key"), "org": org, "event_type": "DELIVERY_RUNG_ADVANCED", "summary": f"{org} advanced verified rung {event.get('event_type')}", "severity": "P2"})
            for c in self._contradictions.get(org, []):
                events.append({"idempotency_key": c.get("event_id"), "org": org, "event_type": "CONTRADICTION_DETECTED", "summary": "Contradiction detected", "severity": "P0"})
        for item in aftercare:
            events.append({"idempotency_key": _sha256({"aftercare": item}), "org": item.get("org"), "event_type": "CRITICAL_PR_MERGED_AFTERCARE_MISSING", "summary": "Critical PR merged but aftercare is missing", "severity": "P0"})
        deduped = {e["idempotency_key"]: e for e in events if e.get("idempotency_key")}
        path = self.root / "notifications" / "all-orgs" / "material-events.jsonl"
        _write_jsonl(path, list(deduped.values()))
        return {"schema": "org.material_notifications_projection.v1", "computed_at": _now(), "material_events": list(deduped.values())}

    def _scorecard(self, orgs: list[str]) -> dict[str, Any]:
        rows = []
        for org in orgs:
            candidate = len(list((self.root / "inbox" / org / "candidate").glob("*.json")))
            events = len(self._load_events(org))
            debt = len(self._debts.get(org, []))
            contradictions = len(self._contradictions.get(org, []))
            risk = debt + contradictions + max(0, candidate - events)
            rows.append({"org": org, "runtime_health": "unknown", "activity": candidate + events + debt, "candidate_progress": candidate, "verified_movement": events, "blocker_closure": 0, "delivery_movement": events, "outcome_movement": 0, "risk": risk, "activity_to_verified_gap": (candidate + debt) - events})
        rows.sort(key=lambda row: row["activity_to_verified_gap"], reverse=True)
        return {"schema": "org.anti_theater_scorecard.v1", "computed_at": _now(), "orgs": rows, "rules": ["activity_never_equals_delivery", "candidate_progress_never_equals_verified_movement", "verified_movement_does_not_imply_accepted_or_landed"]}

    def _release_memory(self, orgs: list[str]) -> dict[str, Any]:
        all_debts = [d | {"org": org} for org in orgs for d in self._debts.get(org, [])]
        stale = {org: self._stale_receipts.get(org, []) for org in orgs if self._stale_receipts.get(org)}
        merged_without_aftercare = [d for d in all_debts if d.get("debt_type") == "POST_MERGE_VALIDATION_MISSING"]
        common_debt: dict[str, int] = {}
        for debt in all_debts:
            common_debt[debt["debt_type"]] = common_debt.get(debt["debt_type"], 0) + 1
        return {"schema": RELEASE_MEMORY_SCHEMA, "computed_at": _now(), "orgs": orgs, "questions": {"which_agents_produced_verified_movement": {}, "which_lanes_produced_candidate_only_churn": {}, "which_claims_were_most_often_rejected": self._rejected_receipts, "which_proof_types_were_most_often_missing": common_debt, "which_prs_sat_longest_without_current_head_checks": [d for d in all_debts if d.get("debt_type", "").startswith("REQUIRED_CHECK")], "which_prs_sat_longest_without_sidecar_receipts": [d for d in all_debts if "SIDECAR" in d.get("debt_type", "")], "which_prs_merged_without_aftercare": merged_without_aftercare, "which_capabilities_are_merged_but_not_enabled": [], "which_capabilities_are_enabled_but_not_deployed": [], "which_capabilities_are_deployed_but_not_accepted": [], "which_blockers_reopened": [], "which_protected_boundaries_recur": [d for d in all_debts if d.get("protected_boundary")], "which_org_has_highest_activity_to_verified_movement_gap": self._scorecard(orgs).get("orgs", [])[:1], "repeated_stale_receipts": stale}}

    def _write_verifier_run(self, org: str) -> None:
        run = {"schema": "org.verifier_result.v1", "org": org, "verifier": VERIFIER_NAME, "verifier_version": VERIFIER_VERSION, "policy_version_hash": self._policy_hash(org), "live_state_hash": _sha256(self._live_state.get(org, {})), "result": self._results.get(org, self._empty_result()), "promoted_event_ids": self._promoted_ids.get(org, []), "rejected_receipt_ids": self._rejected_receipts.get(org, []), "stale_receipt_ids": self._stale_receipts.get(org, []), "proof_debt_ids": [d.get("debt_id") for d in self._debts.get(org, [])], "contradiction_ids": [c.get("event_id") for c in self._contradictions.get(org, [])], "projection_hashes": {"product_progress": _read_json(self.root / "projections" / org / "product-progress-proof.json").get("projection_hash")}}
        _write_json(self.root / "verifier-runs" / org / f"{_now().replace(':', '-')}.json", run)

    def assign_dispatch_ticket(self, ticket_id: str, lane: str) -> dict[str, Any]:
        ticket = self._find_ticket(ticket_id)
        if ticket.get("missing"):
            return {"schema": DISPATCH_ASSIGNMENT_SCHEMA, "assignment_id": "asg_missing_" + hashlib.sha256(ticket_id.encode()).hexdigest()[:16], "ticket_id": ticket_id, "org": "unknown", "lane": lane, "status": "rejected", "reason": "unknown_ticket_id"}
        org = str(ticket.get("org") or "unknown")
        invalid = set(ticket.get("invalid_lanes", []))
        eligible = set(ticket.get("eligible_lanes", []))
        status = "assigned" if lane not in invalid and (not eligible or lane in eligible) else "rejected"
        assignment = {"schema": DISPATCH_ASSIGNMENT_SCHEMA, "assignment_id": "asg_" + hashlib.sha256(f"{ticket_id}:{lane}".encode()).hexdigest()[:24], "ticket_id": ticket_id, "org": org, "lane": lane, "status": status, "assigned_at": _now(), "non_claims": ["assignment_does_not_imply_readiness"]}
        root = self.root / "dispatch" / ("assigned" if status == "assigned" else "rejected") / org
        if status == "assigned":
            root = root / lane
        _write_json(root / f"{assignment['assignment_id']}.json", assignment)
        self._refresh_dispatch_state_files()
        return assignment

    def close_dispatch_ticket(self, ticket_id: str, *, evidence_event_required: bool = True, reason: str = "verified_evidence_promoted") -> dict[str, Any]:
        ticket = self._find_ticket(ticket_id)
        if ticket.get("missing"):
            return {"schema": DISPATCH_ASSIGNMENT_SCHEMA, "assignment_id": "close_missing_" + hashlib.sha256(ticket_id.encode()).hexdigest()[:16], "ticket_id": ticket_id, "org": "unknown", "lane": "verifier", "status": "rejected", "reason": "unknown_ticket_id"}
        org = str(ticket.get("org") or "unknown")
        closure = {"schema": DISPATCH_ASSIGNMENT_SCHEMA, "assignment_id": "close_" + hashlib.sha256(ticket_id.encode()).hexdigest()[:24], "ticket_id": ticket_id, "org": org, "lane": "verifier", "status": "closed", "closed_at": _now(), "reason": reason, "evidence_event_required": evidence_event_required}
        _write_json(self.root / "dispatch" / "closed" / org / f"{closure['assignment_id']}.json", closure)
        self._refresh_dispatch_state_files()
        return closure

    def _find_ticket(self, ticket_id: str) -> dict[str, Any]:
        tickets = _read_json(self.root / "projections" / "all-orgs" / "dispatch-tickets.json").get("tickets", [])
        for ticket in tickets:
            if ticket.get("ticket_id") == ticket_id:
                return dict(ticket)
        for path in (self.root / "dispatch" / "assigned").glob("*/*/*.json"):
            assignment = _read_json(path)
            if assignment.get("ticket_id") == ticket_id:
                return {"ticket_id": ticket_id, "org": assignment.get("org"), "eligible_lanes": [assignment.get("lane")], "invalid_lanes": []}
        return {"ticket_id": ticket_id, "org": "unknown", "eligible_lanes": [], "invalid_lanes": ["CEO", "founder"], "missing": True}

    def _refresh_dispatch_state_files(self) -> None:
        orgs = sorted(set(self.policies) | {p.name for p in (self.root / "projections").iterdir() if p.is_dir() and p.name != "all-orgs"} if (self.root / "projections").exists() else set(self.policies))
        if orgs:
            _write_json(self.root / "projections" / "all-orgs" / "dispatch-state.json", self._dispatch_state(orgs))
            for org in orgs:
                _write_json(self.root / "projections" / org / "dispatch-state.json", self._dispatch_state([org]))

    def _move(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
