#!/usr/bin/env python3
"""CLI helper for the org verified evidence fabric.

The command intentionally mutates only repo/local state directories.  Production
cron, branch protection, and external notifications remain operator-controlled
unless installed by separate repo-native config.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from neoengine_local.org_evidence import OrgEvidenceFabric, _schema_missing, write_agent_closeout_receipt


def _load_json(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        with Path(path).expanduser().open(encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def _load_policies(values: list[str]) -> dict[str, dict[str, Any]]:
    policies: dict[str, dict[str, Any]] = {}
    policy_values = list(values)
    if not policy_values:
        policy_values = [str(path) for path in sorted((REPO_ROOT / "neoengine_local" / "org_evidence_policies").glob("*.policy.json"))]
    for value in policy_values:
        if "=" in value:
            org, raw_path = value.split("=", 1)
            policy = _load_json(raw_path)
            policies[org] = policy
        else:
            policy = _load_json(value)
            org = str(policy.get("org") or Path(value).stem.split(".")[0])
            policies[org] = policy
    return dict(sorted(policies.items()))


def _fabric(args: argparse.Namespace) -> OrgEvidenceFabric:
    return OrgEvidenceFabric(Path(args.root).expanduser(), policies=_load_policies(getattr(args, "policy", [])))


def receipt_command(args: argparse.Namespace) -> int:
    subject = {"type": "pull_request", "number": args.pr, "head_sha": args.head, "base_sha": args.base}
    evidence = {"head_sha": args.head, "base_sha": args.base}
    if args.evidence_json:
        evidence.update(_load_json(args.evidence_json))
    claims: list[Mapping[str, Any]] = []
    for claim in args.claim:
        claims.append({"type": claim, "status": "candidate", "evidence": evidence})
    path = write_agent_closeout_receipt(
        Path(args.root).expanduser(),
        org=args.org,
        repo=args.repo,
        lane=args.lane,
        agent=args.agent,
        work_packet=args.work_packet,
        subject=subject,
        claims=claims,
        non_claims=args.non_claim,
        protected_boundary=args.protected_boundary,
        human_decision_required=args.human_decision_required,
    )
    print(path)
    return 0


def verify_command(args: argparse.Namespace) -> int:
    result = _fabric(args).verify_all(live_state=_load_json(args.live_state), dry_run=args.dry_run)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def policy_list_command(args: argparse.Namespace) -> int:
    policies = _load_policies(args.policy)
    print(json.dumps({"schema": "org.policy_list.v1", "orgs": sorted(policies), "count": len(policies)}, indent=2, sort_keys=True))
    return 0


def policy_validate_command(args: argparse.Namespace) -> int:
    policies = _load_policies(args.policy)
    results = {org: {"valid": not _schema_missing(policy, "org.policy.v1"), "missing": _schema_missing(policy, "org.policy.v1")} for org, policy in policies.items()}
    print(json.dumps({"schema": "org.policy_validation.v1", "results": results}, indent=2, sort_keys=True))
    return 0 if all(item["valid"] for item in results.values()) else 2


def dispatch_list_command(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser()
    data = _load_json(str(root / "projections" / "all-orgs" / "dispatch-tickets.json"))
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def dispatch_assign_command(args: argparse.Namespace) -> int:
    result = _fabric(args).assign_dispatch_ticket(args.ticket_id, args.lane)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "assigned" else 2


def dispatch_close_command(args: argparse.Namespace) -> int:
    result = _fabric(args).close_dispatch_ticket(args.ticket_id, evidence_event_required=not args.no_evidence_required, reason=args.reason)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def dispatch_reconcile_command(args: argparse.Namespace) -> int:
    verify_args = argparse.Namespace(**vars(args))
    verify_args.dry_run = False
    result = _fabric(verify_args).verify_all(live_state=_load_json(args.live_state))
    state = _load_json(str(Path(args.root).expanduser() / "projections" / "all-orgs" / "dispatch-state.json"))
    print(json.dumps({"verify": result, "dispatch_state": state}, indent=2, sort_keys=True))
    return 0


def gate_evaluate_command(args: argparse.Namespace) -> int:
    if args.live_state:
        _fabric(args).verify_all(live_state=_load_json(args.live_state))
    data = _load_json(str(Path(args.root).expanduser() / "projections" / "all-orgs" / "release-gates.json"))
    print(json.dumps(data, indent=2, sort_keys=True))
    return 2 if any(gate.get("result") != "pass" for gate in data.get("gates", [])) else 0


def projection_command(name: str, path: str):
    def _cmd(args: argparse.Namespace) -> int:
        if getattr(args, "live_state", None):
            _fabric(args).verify_all(live_state=_load_json(args.live_state))
        data = _load_json(str(Path(args.root).expanduser() / path))
        print(json.dumps(data or {"schema": f"org.{name}.v1", "missing": path}, indent=2, sort_keys=True))
        return 0
    return _cmd


def doctor_command(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser()
    policies = _load_policies(args.policy)
    policy_errors = {org: _schema_missing(policy, "org.policy.v1") for org, policy in policies.items() if _schema_missing(policy, "org.policy.v1")}
    checks = {
        "schema": "org.evidence_doctor.v1",
        "policy_load": sorted(policies),
        "policy_errors": policy_errors,
        "writable_state_directories": root.exists() or root.parent.exists(),
        "malformed_receipts": len(list((root / "inbox").glob("*/malformed/*.json"))) if (root / "inbox").exists() else 0,
        "stale_receipts": len(list((root / "stale").glob("*/*.json"))) if (root / "stale").exists() else 0,
        "open_proof_debt": _load_json(str(root / "projections" / "all-orgs" / "watchdog-summary.json")).get("totals", {}).get("proof_debt_open", 0),
        "contradictions": _load_json(str(root / "projections" / "all-orgs" / "watchdog-summary.json")).get("totals", {}).get("contradictions", 0),
        "dispatch_backlog": _load_json(str(root / "projections" / "all-orgs" / "dispatch-tickets.json")).get("ticket_count", 0),
        "missing_all_org_projections": [p for p in ["watchdog-summary.json", "release-gates.json", "dispatch-state.json", "release-memory.json"] if not (root / "projections" / "all-orgs" / p).exists()],
    }
    print(json.dumps(checks, indent=2, sort_keys=True))
    return 2 if policy_errors or checks["open_proof_debt"] or checks["contradictions"] or checks["missing_all_org_projections"] else 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default="~/.hermes/state/org-evidence")
    parser.add_argument("--policy", action="append", default=[], help="policy file or org=policy-file; omitted means all seeded policies")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-progress", description="Write and verify org delivery evidence receipts")
    sub = parser.add_subparsers(dest="command", required=True)

    receipt = sub.add_parser("receipt", help="write an agent candidate closeout receipt")
    receipt.add_argument("--root", default="~/.hermes/state/org-evidence")
    receipt.add_argument("--org", required=True)
    receipt.add_argument("--repo", required=True)
    receipt.add_argument("--pr", required=True, type=int)
    receipt.add_argument("--head", required=True)
    receipt.add_argument("--base", required=True)
    receipt.add_argument("--lane", required=True)
    receipt.add_argument("--agent", required=True)
    receipt.add_argument("--work-packet", required=True)
    receipt.add_argument("--claim", action="append", required=True)
    receipt.add_argument("--non-claim", action="append", default=[])
    receipt.add_argument("--evidence-json")
    receipt.add_argument("--protected-boundary", action="store_true")
    receipt.add_argument("--human-decision-required", action="store_true")
    receipt.set_defaults(func=receipt_command)

    verify = sub.add_parser("verify", help="promote/reject/stale candidate receipts and write projections")
    add_common(verify)
    verify.add_argument("--live-state", help="JSON live-state fixture produced by GitHub/runtime collectors")
    verify.add_argument("--dry-run", action="store_true")
    verify.set_defaults(func=verify_command)

    policy = sub.add_parser("policy", help="policy registry commands")
    psub = policy.add_subparsers(dest="policy_command", required=True)
    plist = psub.add_parser("list")
    plist.add_argument("--policy", action="append", default=[])
    plist.set_defaults(func=policy_list_command)
    pval = psub.add_parser("validate")
    pval.add_argument("--policy", action="append", default=[])
    pval.set_defaults(func=policy_validate_command)

    watchdog = sub.add_parser("watchdog", help="watchdog projection commands")
    wsub = watchdog.add_subparsers(dest="watchdog_command", required=True)
    ws = wsub.add_parser("summarize")
    add_common(ws)
    ws.add_argument("--live-state")
    ws.set_defaults(func=projection_command("watchdog_summary", "projections/all-orgs/watchdog-summary.json"))

    dispatch = sub.add_parser("dispatch", help="durable dispatch commands")
    dsub = dispatch.add_subparsers(dest="dispatch_command", required=True)
    dl = dsub.add_parser("list")
    dl.add_argument("--root", default="~/.hermes/state/org-evidence")
    dl.set_defaults(func=dispatch_list_command)
    da = dsub.add_parser("assign")
    add_common(da)
    da.add_argument("ticket_id")
    da.add_argument("lane")
    da.set_defaults(func=dispatch_assign_command)
    dc = dsub.add_parser("close")
    add_common(dc)
    dc.add_argument("ticket_id")
    dc.add_argument("--reason", default="verified_evidence_promoted")
    dc.add_argument("--no-evidence-required", action="store_true")
    dc.set_defaults(func=dispatch_close_command)
    dr = dsub.add_parser("reconcile")
    add_common(dr)
    dr.add_argument("--live-state")
    dr.set_defaults(func=dispatch_reconcile_command)

    gate = sub.add_parser("gate", help="release gate commands")
    gsub = gate.add_subparsers(dest="gate_command", required=True)
    ge = gsub.add_parser("evaluate")
    add_common(ge)
    ge.add_argument("--live-state")
    ge.set_defaults(func=gate_evaluate_command)

    mem = sub.add_parser("memory", help="release memory commands")
    msub = mem.add_subparsers(dest="memory_command", required=True)
    ms = msub.add_parser("summarize")
    add_common(ms)
    ms.add_argument("--live-state")
    ms.set_defaults(func=projection_command("release_memory", "projections/all-orgs/release-memory.json"))

    graph = sub.add_parser("graph", help="graph commands")
    g2sub = graph.add_subparsers(dest="graph_command", required=True)
    gb = g2sub.add_parser("build")
    add_common(gb)
    gb.add_argument("--live-state")
    gb.set_defaults(func=projection_command("progress_graph", "projections/all-orgs/progress-graph.json"))

    notif = sub.add_parser("notifications", help="material notification commands")
    nsub = notif.add_subparsers(dest="notifications_command", required=True)
    nl = nsub.add_parser("list")
    add_common(nl)
    nl.add_argument("--live-state")
    nl.set_defaults(func=projection_command("material_notifications", "projections/all-orgs/material-notifications.json"))

    doctor = sub.add_parser("doctor", help="diagnose policy/schema/debt/gate/dispatch state")
    add_common(doctor)
    doctor.set_defaults(func=doctor_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
