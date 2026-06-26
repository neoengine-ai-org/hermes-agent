#!/usr/bin/env python3
"""CLI helper for the org verified evidence fabric.

Examples:
  hermes-progress receipt --root ~/.hermes/state/org-evidence --org neoengine \
    --repo neoengine-ai-org/neoengine --pr 726 --head abc --base def \
    --lane NE-SONNET-01 --agent sonnet --work-packet roadmap-os-726-closeout \
    --claim PR_REBASED --non-claim not_accepted --non-claim not_landed

  hermes-progress verify --root ~/.hermes/state/org-evidence \
    --policy neoengine=neoengine_local/org_evidence_policies/neoengine.policy.json \
    --live-state /tmp/live-state.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from neoengine_local.org_evidence import OrgEvidenceFabric, write_agent_closeout_receipt


def _load_json(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    with Path(path).expanduser().open() as fh:
        data = json.load(fh)
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
    return policies


def receipt_command(args: argparse.Namespace) -> int:
    subject = {"type": "pull_request", "number": args.pr, "head_sha": args.head, "base_sha": args.base}
    evidence = {"head_sha": args.head, "base_sha": args.base}
    if args.evidence_json:
        evidence.update(_load_json(args.evidence_json))
    path = write_agent_closeout_receipt(
        Path(args.root).expanduser(),
        org=args.org,
        repo=args.repo,
        lane=args.lane,
        agent=args.agent,
        work_packet=args.work_packet,
        subject=subject,
        claims=[{"type": claim, "status": "candidate", "evidence": evidence} for claim in args.claim],
        non_claims=args.non_claim,
        protected_boundary=args.protected_boundary,
        human_decision_required=args.human_decision_required,
    )
    print(path)
    return 0


def verify_command(args: argparse.Namespace) -> int:
    fabric = OrgEvidenceFabric(Path(args.root).expanduser(), policies=_load_policies(args.policy))
    result = fabric.verify_all(live_state=_load_json(args.live_state))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


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
    verify.add_argument("--root", default="~/.hermes/state/org-evidence")
    verify.add_argument("--policy", action="append", default=[], help="policy file or org=policy-file; repeat for all orgs")
    verify.add_argument("--live-state", help="JSON live-state fixture produced by GitHub/runtime collectors")
    verify.set_defaults(func=verify_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
