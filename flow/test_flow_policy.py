import copy
import json
import tempfile
import unittest
from pathlib import Path

from flow_policy import load_policy, rulesets


def example_policy():
    return {
        "version": 1, "gate_app_id": 100, "merger_app_id": 200,
        "promotion_checks": ["ci-lint", "ci-unit", "ci-heavy"],
        "check_sources": {name: 15368 for name in ("ci-lint", "ci-unit", "ci-heavy")},
        "reviewers": {name: {
            "soft": {"files": 80, "lines": 8000, "patch_bytes": 32000, "largest_patch_bytes": 8000},
            "hard": {"files": 100, "lines": 10000, "patch_bytes": 40000, "largest_patch_bytes": 10000},
            "exclude": [],
        } for name in ("qodo", "coderabbit")},
    }


class PolicyTests(unittest.TestCase):
    def parse(self, policy):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(policy))
            return load_policy(path)

    def test_complete_policy_parses(self):
        self.assertEqual(self.parse(example_policy()), example_policy())

    def test_missing_lint_unit_or_heavy_cannot_weaken_main(self):
        for name in ("ci-lint", "ci-unit", "ci-heavy"):
            policy = example_policy()
            policy["promotion_checks"].remove(name)
            with self.assertRaises(ValueError):
                self.parse(policy)

    def test_missing_provider_cannot_silently_disable_review(self):
        policy = example_policy()
        del policy["reviewers"]["qodo"]
        with self.assertRaises(ValueError):
            self.parse(policy)

    def test_global_review_exclusion_is_rejected(self):
        policy = example_policy()
        policy["reviewers"]["qodo"]["exclude"] = ["**/*"]
        with self.assertRaises(ValueError):
            self.parse(policy)

    def test_same_app_can_use_separately_scoped_tokens(self):
        policy = example_policy()
        policy["gate_app_id"] = policy["merger_app_id"]
        self.assertEqual(self.parse(policy)["gate_app_id"], policy["merger_app_id"])

    def test_develop_lint_and_tests_are_not_required(self):
        develop = rulesets(example_policy())[0]
        checks = next(r for r in develop["rules"] if r["type"] == "required_status_checks")
        contexts = {c["context"] for c in checks["parameters"]["required_status_checks"]}
        self.assertEqual(contexts, {"post-commit", "flow-admission"})
        self.assertFalse(checks["parameters"]["strict_required_status_checks_policy"])

    def test_main_requires_full_validation(self):
        main = rulesets(example_policy())[1]
        checks = next(r for r in main["rules"] if r["type"] == "required_status_checks")
        contexts = {c["context"] for c in checks["parameters"]["required_status_checks"]}
        self.assertTrue({"post-commit", "flow-promotion", "ci-lint", "ci-unit", "ci-heavy"} <= contexts)

    def test_merger_bypass_only_applies_to_writer_rule(self):
        develop, main, writer = rulesets(example_policy())
        self.assertEqual(develop["bypass_actors"], [])
        self.assertEqual(main["bypass_actors"], [])
        self.assertEqual(writer["rules"], [{"type": "update"}])
        self.assertEqual(writer["bypass_actors"][0]["bypass_mode"], "pull_request")

    def test_org_rules_target_only_migrated_repos(self):
        for rule in rulesets(example_policy(), ["sdk", "agent"]):
            self.assertEqual(rule["conditions"]["repository_name"]["include"], ["agent", "sdk"])
            self.assertNotIn("~DEFAULT_BRANCH", rule["conditions"]["ref_name"]["include"])

    def test_unscoped_org_rollout_forbidden(self):
        with self.assertRaises(ValueError):
            rulesets(example_policy(), [])
        with self.assertRaises(ValueError):
            rulesets(example_policy(), ["*"])


if __name__ == "__main__":
    unittest.main()
