import io
import json
import unittest
import zipfile
from unittest.mock import patch

from flow_controller import Controller, validation_receipt
from flow_core import Verdict
from test_flow_policy import example_policy


class ValidationAPI:
    def __init__(self):
        self.receipt = {"version": 1, "pr": 1, "head": "h", "base": "b", "merge": "m", "run_id": 9, "attempt": 1}
        self.run_status = "completed"
        self.jobs = [{"name": name, "status": "completed", "conclusion": "success"}
                     for name in ("ci-lint", "ci-unit", "ci-heavy")]

    def pages(self, endpoint, field=None, parameters=None):
        if endpoint.endswith("/runs"):
            return [{"id": 9, "path": ".github/workflows/ci.yml", "status": self.run_status}]
        if endpoint.endswith("artifacts"):
            return [{"id": 4, "name": "flow-candidate-1", "expired": False, "size_in_bytes": 500}]
        return self.jobs

    def request(self, method, endpoint, payload=None, **kwargs):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("flow-candidate.json", json.dumps(self.receipt))
        return stream.getvalue()


class ControllerTests(unittest.TestCase):
    def pr(self, number=1, base="develop"):
        return {"number": number, "state": "open", "draft": False, "base": {"ref": base, "sha": "b"},
                "head": {"sha": "h", "ref": "feature", "repo": {"full_name": "a/b"}}, "merge_commit_sha": "m"}

    def test_receipt_binds_ci_to_head_base_merge_and_attempt(self):
        api = ValidationAPI()
        policy = example_policy() | {"qualification_workflow": ".github/workflows/ci.yml"}
        checks, error = validation_receipt(api, "a/b", self.pr(), policy)
        self.assertFalse(error)
        self.assertEqual(checks["ci-heavy"], "success")
        for field in ("base", "head", "merge", "attempt", "run_id", "pr"):
            original = api.receipt[field]
            api.receipt[field] = "old"
            self.assertEqual(validation_receipt(api, "a/b", self.pr(), policy)[1], "stale-validation")
            api.receipt[field] = original

    def test_latest_running_validation_does_not_reuse_old_success(self):
        api = ValidationAPI()
        api.run_status = "in_progress"
        policy = example_policy() | {"qualification_workflow": ".github/workflows/ci.yml"}
        self.assertEqual(validation_receipt(api, "a/b", self.pr(), policy)[1], "validation-running")

    def test_duplicate_or_skipped_summary_is_not_success(self):
        api = ValidationAPI()
        api.jobs[0]["conclusion"] = "skipped"
        api.jobs.append(dict(api.jobs[1]))
        policy = example_policy() | {"qualification_workflow": ".github/workflows/ci.yml"}
        checks, _ = validation_receipt(api, "a/b", self.pr(), policy)
        self.assertEqual(checks["ci-lint"], "skipped")
        self.assertNotIn("ci-unit", checks)

    def test_no_status_or_merge_mutation_in_dry_run(self):
        class NoWrites:
            def request(self, method, endpoint, payload=None):
                if method != "GET":
                    raise AssertionError("unexpected write")
        self.assertIsNone(Controller(NoWrites(), "a/b").status("sha", "flow-admission", "success", "ok"))

    def test_reviews_requested_once_per_head_base_pair(self):
        class Requests:
            def __init__(self):
                self.comments = []
            def request(self, method, endpoint, payload=None):
                self.comments.append({"user": {"login": "kodflow-ci[bot]"}, "body": payload["body"]})
        api = Requests()
        controller = Controller(api, "a/b", apply=True)
        pr = self.pr()
        controller.request_reviews(pr, [])
        self.assertEqual(len(api.comments), 2)
        controller.request_reviews(pr, api.comments)
        self.assertEqual(len(api.comments), 2)
        pr["head"]["sha"] = "new-head"
        controller.request_reviews(pr, api.comments)
        self.assertEqual(len(api.comments), 4)

    def test_dry_run_never_posts_review_commands(self):
        class NoWrites:
            def request(self, *args, **kwargs):
                raise AssertionError("unexpected write")
        Controller(NoWrites(), "a/b").request_reviews(self.pr(), [])

    def test_only_one_merge_per_snapshot_then_fleet_state_is_refetched(self):
        policy = example_policy() | {"managed_files": {}, "qualification_workflow": ".github/workflows/ci.yml"}
        first, second = self.pr(1), self.pr(2)
        class Merges:
            def __init__(self):
                self.merged = []
            def pages(self, endpoint, **kwargs):
                return [first, second]
            def request(self, method, endpoint, payload=None):
                if method == "PUT":
                    self.merged.append(endpoint)
                    return {"merged": True}
                return first if endpoint.endswith("/1") else second
        api = Merges()
        controller = Controller(api, "a/b", apply=True)
        with patch("flow_controller.contents", return_value=(json.dumps(policy).encode(), "blob")), \
                patch.object(controller, "writer_locked", return_value=True), \
                patch.object(controller, "evaluate", return_value=("flow-admission", Verdict(True, "within-budget"), ("m", "d"))) as evaluate, \
                patch.object(controller, "status"), patch.object(controller, "fresh", return_value=True), \
                patch("flow_controller.authorized_label", return_value=True):
            result = controller.run()
        self.assertEqual(len(api.merged), 1)
        self.assertEqual(evaluate.call_count, 1)
        self.assertTrue(result[0]["merged"])

    def test_missing_exclusive_writer_rule_prevents_merging(self):
        class Empty:
            def pages(self, endpoint):
                return []
        self.assertFalse(Controller(Empty(), "a/b").writer_locked(example_policy()))

    def test_writer_bypass_must_not_bypass_other_rules(self):
        policy = example_policy()
        class Wrong:
            def pages(self, endpoint):
                return [{"type": "update", "ruleset_id": 1, "ruleset_source_type": "Repository"}]
            def request(self, method, endpoint, payload=None):
                return {"name": "flow-writer", "enforcement": "active", "bypass_actors": [{
                    "actor_type": "Integration", "actor_id": policy["merger_app_id"], "bypass_mode": "pull_request"}],
                    "rules": [{"type": "update"}, {"type": "required_status_checks"}]}
        self.assertFalse(Controller(Wrong(), "a/b").writer_locked(policy))

    def test_freshness_rechecks_freeze_before_develop_merge(self):
        pr = self.pr()
        promotion = self.pr(2, "main")
        promotion["head"]["ref"] = "develop"
        class Fresh:
            def request(self, method, endpoint, payload=None):
                if "/pulls/" in endpoint:
                    return pr
                return {"object": {"sha": "main" if endpoint.endswith("main") else "develop"}}
            def pages(self, endpoint, **kwargs):
                return [promotion]
        with patch("flow_controller.authorized_label", side_effect=lambda a, r, p, label: label == "flow:merge"):
            self.assertFalse(Controller(Fresh(), "a/b").fresh(pr, "main", "develop"))


if __name__ == "__main__":
    unittest.main()
