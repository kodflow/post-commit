import unittest

from flow_controller import external_e2e_receipt


class ExternalTests(unittest.TestCase):
    def setUp(self):
        self.spec = {"dispatch_job": "dispatch", "context": "e2e/runner-template", "bot_login": "kodflow-ci[bot]",
                     "repository": "a/runner-template", "workflow": ".github/workflows/e2e.yml"}
        self.jobs = [{"name": "dispatch", "conclusion": "success"}]
        self.source = {"created_at": "2026-10-22T10:00:00Z", "run_started_at": "2026-10-22T11:00:00Z"}
        self.statuses = [{"id": 1, "context": self.spec["context"], "state": "success", "creator": {"login": self.spec["bot_login"]},
                          "created_at": "2026-10-22T11:01:00Z", "target_url": "https://github.com/a/runner-template/actions/runs/99"}]
        self.run = {"event": "repository_dispatch", "path": self.spec["workflow"], "status": "completed", "conclusion": "success", "created_at": "2026-10-22T11:00:01Z"}
        outer = self
        class API:
            def pages(self, endpoint):
                outer.assertEqual(endpoint, "repos/a/b/commits/merge/statuses")
                return outer.statuses
            def request(self, method, endpoint):
                outer.assertEqual(endpoint, "repos/a/runner-template/actions/runs/99")
                return outer.run
        self.api = API()

    def verdict(self):
        return external_e2e_receipt(self.api, "a/b", "merge", self.source, self.jobs, self.spec)

    def test_success_requires_external_completed_run_on_merge_candidate(self):
        self.assertEqual(self.verdict(), "")
        self.run["status"] = "in_progress"
        self.assertEqual(self.verdict(), "external-e2e-run-incomplete")

    def test_missing_failed_pending_or_spoofed_status_blocks(self):
        original = dict(self.statuses[0])
        for state in ("pending", "failure", "error"):
            self.statuses[0] = original | {"state": state}
            self.assertTrue(self.verdict())
        self.statuses[0] = original | {"creator": {"login": "stranger"}}
        self.assertTrue(self.verdict())
        self.statuses = []
        self.assertTrue(self.verdict())

    def test_latest_failure_overrides_older_success(self):
        self.statuses.append(self.statuses[0] | {"id": 2, "state": "failure"})
        self.assertTrue(self.verdict())

    def test_old_attempt_or_unexpected_workflow_blocks(self):
        self.statuses[0]["created_at"] = "2026-10-22T10:59:59Z"
        self.assertTrue(self.verdict())
        self.statuses[0]["created_at"] = "2026-10-22T11:01:00Z"
        self.run["path"] = ".github/workflows/other.yml"
        self.assertTrue(self.verdict())

    def test_only_explicitly_skipped_dispatch_can_omit_external_result(self):
        self.statuses = []
        self.jobs[0]["conclusion"] = "skipped"
        self.assertEqual(self.verdict(), "")
        self.jobs = []
        self.assertTrue(self.verdict())

    def test_untrusted_target_url_never_read(self):
        self.statuses[0]["target_url"] = "https://github.com/other/repo/actions/runs/99"
        self.assertTrue(self.verdict())
