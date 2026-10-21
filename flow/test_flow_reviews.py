import unittest

from flow_reviews import evidence


class ReviewAdapterTests(unittest.TestCase):
    def args(self):
        return dict(head="a" * 40, base="b" * 40, anchor_time="2026-10-21T10:00:00Z",
                    expected_paths=["src/a.py"], reviews=[], comments=[], statuses=[], unresolved=[])

    def review(self, body, **updates):
        return {"user": {"login": "coderabbitai[bot]"}, "commit_id": "a" * 40,
                "submitted_at": "2026-10-21T11:00:00Z", "state": "COMMENTED", "body": body, **updates}

    def cr_body(self, count=0, paths="* `src/a.py`", number=1):
        return f"**Actionable comments posted: {count}**\n<summary>📒 Files selected for processing: {number}</summary>\n{paths}\n</details>"

    def test_coderabbit_explicit_zero_findings_and_file_list(self):
        result = evidence("coderabbit", **(self.args() | {"reviews": [self.review(self.cr_body())]}))
        self.assertTrue(result.completed and result.complete_coverage)
        self.assertEqual(result.blocking_findings, 0)

    def test_coderabbit_success_status_without_review_does_not_pass(self):
        result = evidence("coderabbit", **(self.args() | {
            "statuses": [{"context": "CodeRabbit", "state": "success", "description": "Review rate limited"}]}))
        self.assertFalse(result.completed)
        self.assertEqual(result.error, "rate-limited")

    def test_incremental_review_missing_unchanged_files_is_partial(self):
        args = self.args() | {"expected_paths": ["src/a.py", "src/previous.py"],
                              "reviews": [self.review(self.cr_body())]}
        self.assertFalse(evidence("coderabbit", **args).complete_coverage)

    def test_old_review_does_not_validate_new_base(self):
        review = self.review(self.cr_body(), submitted_at="2026-10-21T09:00:00Z")
        self.assertFalse(evidence("coderabbit", **(self.args() | {"reviews": [review]})).completed)

    def test_findings_or_changes_requested_stay_blocking(self):
        for review in (self.review(self.cr_body(2)), self.review(self.cr_body(), state="CHANGES_REQUESTED")):
            result = evidence("coderabbit", **(self.args() | {"reviews": [review]}))
            self.assertGreater(result.blocking_findings, 0)

    def test_unresolved_threads_stay_blocking(self):
        result = evidence("coderabbit", **(self.args() | {"reviews": [self.review(self.cr_body())], "unresolved": [1]}))
        self.assertFalse(result.completed)

    def test_qodo_comment_must_link_exact_commit_and_clear_findings(self):
        comment = {"user": {"login": "qodo-code-review[bot]"}, "updated_at": "2026-10-21T11:00:00Z",
                   "body": "No findings detected\n`src/a.py`\nReview updated until commit https://github.com/a/b/commit/" + "a" * 40}
        result = evidence("qodo", **(self.args() | {"comments": [comment]}))
        self.assertTrue(result.completed and result.complete_coverage)
        comment["body"] = "No findings detected\n`src/a.py`"
        self.assertFalse(evidence("qodo", **(self.args() | {"comments": [comment]})).completed)

    def test_unknown_vendor_output_is_pending(self):
        result = evidence("coderabbit", **(self.args() | {"reviews": [self.review("All good")] }))
        self.assertEqual(result.error, "unknown-review-format")

    def test_qodo_previous_zero_findings_does_not_clear_current_findings(self):
        comment = {"user": {"login": "qodo-code-review[bot]"}, "updated_at": "2026-10-21T11:00:00Z",
                   "body": "Current review has findings\n`src/a.py`\n<summary>Previous reviews</summary>\nNo findings detected\nReview updated until commit " + "a" * 40}
        self.assertFalse(evidence("qodo", **(self.args() | {"comments": [comment]})).completed)


if __name__ == "__main__":
    unittest.main()
