#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

import check_org_policy_rules_hash as check


class OrgPolicyRulesHashTest(unittest.TestCase):
    def write_rules(self, payload):
        td = tempfile.TemporaryDirectory()
        path = Path(td.name) / "rules.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.addCleanup(td.cleanup)
        return path

    def test_current_vendored_rules_match_expected_identity(self):
        rules = check.load_rules(Path("generated/org-rails-policy-rules.json"))
        result = check.validate_rules(rules, check.DEFAULT_EXPECTED_VERSION, check.DEFAULT_EXPECTED_HASH)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["failures"], [])

    def test_hash_drift_fails_closed(self):
        path = self.write_rules({"policy_version": check.DEFAULT_EXPECTED_VERSION, "ruleset_hash": "sha256:drift"})
        result = check.validate_rules(check.load_rules(path), check.DEFAULT_EXPECTED_VERSION, check.DEFAULT_EXPECTED_HASH)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failures"][0]["field"], "ruleset_hash")
        self.assertIn("Regenerate or vendor", result["update_instructions"])

    def test_version_drift_fails_closed(self):
        path = self.write_rules({"policy_version": "9.9.9", "ruleset_hash": check.DEFAULT_EXPECTED_HASH})
        result = check.validate_rules(check.load_rules(path), check.DEFAULT_EXPECTED_VERSION, check.DEFAULT_EXPECTED_HASH)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failures"][0]["field"], "policy_version")


if __name__ == "__main__":
    unittest.main()
