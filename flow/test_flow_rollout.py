import unittest

from flow_api import APIError
from flow_rollout import check_release_dependency, install_pr, protect, rollout
from test_flow_policy import example_policy


class RolloutTests(unittest.TestCase):
    def test_downstream_release_cannot_silently_lose_upstream_push(self):
        consumer = {"on": {"workflow_run": {"workflows": ["SDK CI*"]}}}
        with self.assertRaisesRegex(ValueError, "release migration"):
            check_release_dependency(consumer, "name: SDK CI (Bazel)\njobs: {}", {}, [])
        check_release_dependency(consumer, "name: SDK CI (Bazel)\njobs: {}", {"preserve_push": True}, [])
        with self.assertRaises(ValueError):
            check_release_dependency(consumer, "name: Other\njobs: {}", {"preserve_push": True},
                                     ["name: SDK CI E2E\njobs: {}"])

    def test_dry_run_returns_exact_paths_without_mutation(self):
        from unittest.mock import patch
        class ReadOnly:
            def request(self, method, endpoint, payload=None):
                if method != "GET":
                    raise AssertionError("unexpected mutation")
        with patch("flow_rollout.optional_file", return_value=None):
            result = install_pr(ReadOnly(), "a/b", {"file": "new"}, "main")
        self.assertEqual(result, {"state": "would-open-migration", "base": "main", "paths": ["file"]})

    def test_protection_never_installed_before_workflow_producers(self):
        from unittest.mock import patch
        class ReadOnly:
            def request(self, method, endpoint, payload=None):
                self.assertion = method
                return {"default_branch": "main"}
        with patch("flow_rollout.migration_files", return_value=({"workflow": "desired"}, {})), \
                patch("flow_rollout.optional_file", return_value=None), \
                patch("flow_rollout.install_pr", return_value={"state": "would-open-migration"}), \
                patch("flow_rollout.protect", side_effect=AssertionError("too early")):
            result = rollout(ReadOnly(), "a/b", {}, {})
        self.assertEqual(result["state"], "would-open-migration")

    def test_organization_unavailable_falls_back_to_real_repo_rules(self):
        from unittest.mock import patch
        class Org:
            def request(self, method, endpoint, payload=None):
                return {"owner": {"type": "Organization"}}
            def pages(self, endpoint):
                raise APIError(endpoint, 403)
        with patch("flow_rollout.reconcile_rules", return_value=[]) as reconcile:
            result = protect(Org(), "a/b", example_policy())
        self.assertEqual(result["scope"], "repository")
        self.assertEqual(reconcile.call_args.args[1], "repos/a/b/rulesets")

    def test_invalid_org_payload_is_not_hidden_as_plan_limitation(self):
        class Org:
            def request(self, method, endpoint, payload=None):
                return {"owner": {"type": "Organization"}}
            def pages(self, endpoint):
                raise APIError(endpoint, 422)
        with self.assertRaises(APIError):
            protect(Org(), "a/b", example_policy())


if __name__ == "__main__":
    unittest.main()
