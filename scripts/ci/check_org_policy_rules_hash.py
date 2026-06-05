#!/usr/bin/env python3
"""Validate vendored org-rails policy rules identity.

This downstream guard is intentionally narrow: it verifies that a vendored
`generated/org-rails-policy-rules.json` file declares the expected shared policy
version and ruleset hash exported by NeoEngine. It does not interpret or change
policy semantics.
"""
import argparse
import json
import sys
from pathlib import Path

DEFAULT_EXPECTED_VERSION = "0.1.0"
DEFAULT_EXPECTED_HASH = "sha256:447010f4fc71c86e6a8ee93b17004d13430cea95d547afb1dedec650450312eb"


def load_rules(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"rules file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"rules file is not valid JSON: {path}: {exc}") from exc


def validate_rules(rules: dict, expected_version: str, expected_hash: str) -> dict:
    actual_version = rules.get("policy_version")
    actual_hash = rules.get("ruleset_hash")
    failures = []
    if actual_version != expected_version:
        failures.append({"field": "policy_version", "expected": expected_version, "actual": actual_version})
    if actual_hash != expected_hash:
        failures.append({"field": "ruleset_hash", "expected": expected_hash, "actual": actual_hash})
    return {
        "ok": not failures,
        "policy_version": actual_version,
        "ruleset_hash": actual_hash,
        "expected_policy_version": expected_version,
        "expected_ruleset_hash": expected_hash,
        "failures": failures,
        "update_instructions": (
            "Regenerate or vendor generated/org-rails-policy-rules.json from "
            "NeoEngine's policy:org-rails:export-rules output, then rerun this check."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rules", default="generated/org-rails-policy-rules.json")
    parser.add_argument("--expected-version", default=DEFAULT_EXPECTED_VERSION)
    parser.add_argument("--expected-hash", default=DEFAULT_EXPECTED_HASH)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = validate_rules(load_rules(Path(args.rules)), args.expected_version, args.expected_hash)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result["ok"]:
        print(f"org-rails policy rules match {result['expected_policy_version']} {result['expected_ruleset_hash']}")
    else:
        print(json.dumps(result, indent=2, sort_keys=True), file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
