"""Executable counterexamples to the agreed admission rules."""

import unittest

from flow_core import Limits, Review, Volume, admit_integration, admit_promotion


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.caps = {name: Limits(Volume(8, 80, 800, 400), Volume(10, 100, 1000, 500))
                     for name in ("qodo", "coderabbit")}

    def admit(self, before, after, **kwargs):
        return admit_integration(
            {key: before for key in self.caps},
            {key: after for key in self.caps}, self.caps,
            promotion_open=False, correction_authorized=False, **kwargs,
        )

    def test_feature_below_soft_limit(self):
        self.assertTrue(self.admit(Volume(1, 1, 10, 10), Volume(8, 80, 800, 400)).allowed)

    def test_feature_cannot_consume_correction_reserve(self):
        result = self.admit(Volume(8, 80, 800, 400), Volume(9, 81, 810, 400))
        self.assertEqual((result.allowed, result.reason), (False, "soft-limit"))

    def test_authorized_correction_can_add_file_in_reserve(self):
        result = admit_integration(
            {key: Volume(8, 80, 800, 400) for key in self.caps},
            {key: Volume(9, 75, 750, 400) for key in self.caps}, self.caps,
            promotion_open=True, correction_authorized=True,
        )
        self.assertTrue(result.allowed)

    def test_correction_does_not_bypass_hard_limit(self):
        result = admit_integration(
            {key: Volume(10, 90, 900, 450) for key in self.caps},
            {key: Volume(11, 90, 900, 450) for key in self.caps}, self.caps,
            promotion_open=True, correction_authorized=True,
        )
        self.assertEqual(result.reason, "hard-limit")

    def test_same_volume_change_above_soft_limit_allowed(self):
        self.assertTrue(self.admit(Volume(9, 90, 900, 450), Volume(9, 90, 900, 450)).allowed)

    def test_fewer_files_does_not_hide_line_growth(self):
        result = self.admit(Volume(9, 90, 900, 450), Volume(8, 95, 900, 450))
        self.assertFalse(result.allowed)

    def test_old_oversized_develop_can_be_repaired(self):
        result = self.admit(Volume(11, 110, 1100, 550), Volume(11, 105, 1050, 525))
        self.assertTrue(result.allowed)
        self.assertEqual(result.reason, "recovering")

    def test_one_large_line_is_still_blocked(self):
        result = self.admit(Volume(1, 1, 10, 10), Volume(1, 1, 2000, 2000))
        self.assertEqual(result.reason, "hard-limit")

    def test_promotion_freeze_is_not_just_a_budget_check(self):
        result = admit_integration(
            {key: Volume(1, 1, 10, 10) for key in self.caps},
            {key: Volume(1, 1, 10, 10) for key in self.caps}, self.caps,
            promotion_open=True, correction_authorized=False,
        )
        self.assertEqual(result.reason, "promotion-frozen")

    def test_conflict_still_blocks_integration(self):
        result = self.admit(Volume(1, 1, 10, 10), Volume(1, 1, 10, 10), conflicts=True)
        self.assertEqual(result.reason, "merge-conflict")

    def test_governance_remains_blocking(self):
        result = self.admit(Volume(1, 1, 10, 10), Volume(1, 1, 10, 10), governance_passed=False)
        self.assertEqual(result.reason, "governance-failed")

    def test_missing_provider_metrics_is_not_green(self):
        with self.assertRaises(ValueError):
            admit_integration({}, {}, self.caps, promotion_open=False, correction_authorized=False)

    def test_invalid_limits_fail(self):
        with self.assertRaises(ValueError):
            Limits(Volume(11, 80, 800, 400), Volume(10, 100, 1000, 500))

    def test_no_boolean_or_negative_metrics(self):
        for value in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                Volume(value, 1, 1, 1)

    def test_hard_limit_recovery_cannot_shift_growth_to_another_provider(self):
        result = admit_integration(
            {"qodo": Volume(11, 110, 1100, 550), "coderabbit": Volume(1, 1, 10, 10)},
            {"qodo": Volume(11, 105, 1050, 525), "coderabbit": Volume(2, 1, 10, 10)},
            self.caps, promotion_open=True, correction_authorized=True,
        )
        self.assertFalse(result.allowed)


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.args = dict(
            head="candidate", base="main", source_repository="owner/repo",
            repository="owner/repo", source_branch="develop",
            volumes={key: Volume(1, 1, 10, 10) for key in ("qodo", "coderabbit")},
            limits={key: Limits(Volume(8, 80, 800, 400), Volume(10, 100, 1000, 500))
                    for key in ("qodo", "coderabbit")},
            reviews={key: Review("candidate", "main", True, True, 0)
                     for key in ("qodo", "coderabbit")},
            checks={key: "success" for key in ("lint", "unit", "e2e")},
            required_checks=("lint", "unit", "e2e"),
        )

    def test_complete_candidate_qualified(self):
        self.assertTrue(admit_promotion(**self.args).allowed)

    def test_string_false_is_not_accepted_as_review_completion(self):
        with self.assertRaises(ValueError):
            Review("candidate", "main", "false", "false", 0)

    def test_lint_and_unit_are_required_again_on_promotion(self):
        for name in ("lint", "unit", "e2e"):
            for status in (None, "failure", "skipped", "neutral", "cancelled", "pending"):
                with self.subTest(name=name, status=status):
                    args = self.args | {"checks": self.args["checks"] | {name: status}}
                    self.assertEqual(admit_promotion(**args).reason, "validation-incomplete")

    def test_provider_silence_does_not_count_as_review(self):
        args = self.args | {"reviews": {}}
        self.assertEqual(admit_promotion(**args).reason, "missing-review-evidence")

    def test_review_must_match_head_and_base(self):
        for head, base in (("old", "main"), ("candidate", "old-main")):
            args = self.args | {"reviews": self.args["reviews"] | {
                "qodo": Review(head, base, True, True, 0)}}
            self.assertEqual(admit_promotion(**args).reason, "stale-review")

    def test_partial_failed_or_blocking_review_cannot_promote(self):
        for review in (Review("candidate", "main", True, False, 0),
                       Review("candidate", "main", False, True, 0),
                       Review("candidate", "main", True, True, 1),
                       Review("candidate", "main", True, True, 0, "rate-limit")):
            args = self.args | {"reviews": self.args["reviews"] | {"qodo": review}}
            self.assertEqual(admit_promotion(**args).reason, "review-incomplete")

    def test_fork_named_develop_cannot_promote(self):
        args = self.args | {"source_repository": "attacker/repo"}
        self.assertEqual(admit_promotion(**args).reason, "invalid-promotion-source")

    def test_hard_limit_applies_to_every_provider(self):
        args = self.args | {"volumes": self.args["volumes"] | {
            "qodo": Volume(11, 1, 10, 10)}}
        self.assertEqual(admit_promotion(**args).reason, "hard-limit")


if __name__ == "__main__":
    unittest.main()
