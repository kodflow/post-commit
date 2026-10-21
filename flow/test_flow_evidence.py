import unittest

from flow_evidence import provider_blockers


class ProviderEvidenceTests(unittest.TestCase):
    def test_real_coderabbit_green_rate_limit_is_blocking(self):
        # Observed on kodflow/ktn-linter PR 861, head eb06a1a, 2026-10-21.
        statuses = [{"context": "CodeRabbit", "state": "success",
                     "description": "Review rate limited"}]
        self.assertEqual(provider_blockers("coderabbit", [], statuses), ("rate-limited",))

    def test_real_qodo_out_of_credits_is_blocking(self):
        comments = [{"user": {"login": "qodo-code-review[bot]"},
                     "body": "<!-- qodo:billing-blocked -->\nReviews are paused: out of credits."}]
        self.assertEqual(provider_blockers("qodo", comments, []), ("billing-blocked",))

    def test_coderabbit_walkthrough_marker_is_blocking(self):
        comments = [{"user": {"login": "coderabbitai[bot]"},
                     "body": "<!-- rate limited by coderabbit.ai -->\nReview limit reached"}]
        self.assertEqual(provider_blockers("coderabbit", comments, []), ("rate-limited",))

    def test_human_quote_does_not_impersonate_bot(self):
        comments = [{"user": {"login": "human"}, "body": "<!-- qodo:billing-blocked -->"}]
        self.assertEqual(provider_blockers("qodo", comments, []), ())

    def test_newer_explicit_review_supersedes_old_billing_notice(self):
        comments = [{"user": {"login": "qodo-code-review[bot]"},
                     "updated_at": "2026-10-20T12:00:00Z", "body": "<!-- qodo:billing-blocked -->"}]
        self.assertEqual(provider_blockers("qodo", comments, [],
                                          reviewed_at="2026-10-21T12:00:00Z"), ())

    def test_new_quota_failure_not_hidden_by_old_review(self):
        statuses = [{"context": "CodeRabbit", "state": "success",
                     "description": "Review rate limited", "updated_at": "2026-10-21T12:00:00Z"}]
        self.assertEqual(provider_blockers("coderabbit", [], statuses,
                                          reviewed_at="2026-10-20T12:00:00Z"), ("rate-limited",))

    def test_undated_failure_not_discarded_as_old(self):
        statuses = [{"context": "CodeRabbit", "description": "Review rate limited"}]
        self.assertEqual(provider_blockers("coderabbit", [], statuses,
                                          reviewed_at="2026-10-21T12:00:00Z"),
                         ("unverifiable-timestamp",))

    def test_no_negative_evidence_does_not_create_positive_receipt(self):
        self.assertEqual(provider_blockers("coderabbit", [], []), ())
        # The caller still needs explicit Review evidence, tested in core.


if __name__ == "__main__":
    unittest.main()
