import unittest

from flow_paths import matches


class PathFilterTests(unittest.TestCase):
    def test_double_star_can_match_zero_or_many_directories(self):
        for name in ("go.sum", "a/go.sum", "a/b/go.sum"):
            self.assertTrue(matches(name, "**/go.sum"))

    def test_single_star_does_not_hide_nested_source(self):
        self.assertTrue(matches("src/a.go", "src/*.go"))
        self.assertFalse(matches("src/nested/a.go", "src/*.go"))

    def test_subtree_exclusion_and_root_anchoring(self):
        self.assertTrue(matches("generated/a/b.txt", "generated/**"))
        self.assertFalse(matches("src/generated/a.txt", "generated/**"))


if __name__ == "__main__":
    unittest.main()
