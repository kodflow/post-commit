"""Budget calculations against real Git objects and merges, never mocked diffs."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from flow_git import GitError, MergeConflict, Repository


class GitBudgetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", "/dev/null")
        self.git("config", "core.excludesFile", "/dev/null")
        self.write("seed", "initial\n")
        self.commit()
        self.git("branch", "develop")
        self.git("checkout", "-q", "-b", "feature")
        self.repo = Repository(self.path)
        self.providers = {"qodo": [], "coderabbit": []}

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.path), *args], check=True,
                              capture_output=True).stdout.decode().strip()

    def write(self, name, content):
        path = self.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "test: change")

    def snapshot(self):
        return self.repo.simulate("main", "develop", "feature", self.providers)

    def test_new_file_and_deleted_lines_are_measured(self):
        self.write("seed", "replacement\n")
        self.write("new.txt", "new\n")
        self.commit()
        result = self.snapshot()
        self.assertEqual(result.before["qodo"].files, 0)
        self.assertEqual(result.after["qodo"].files, 2)
        self.assertEqual(result.after["qodo"].lines, 3)
        self.assertGreater(result.after["qodo"].patch_bytes, 20)

    def test_merge_simulation_combines_both_branches_without_checkout(self):
        self.write("feature.txt", "feature\n")
        self.commit()
        self.git("checkout", "-q", "develop")
        self.write("integration.txt", "integration\n")
        self.commit()
        head_before = self.git("rev-parse", "HEAD")
        snapshot = self.snapshot()
        self.assertEqual(snapshot.before["qodo"].files, 1)
        self.assertEqual(snapshot.after["qodo"].files, 2)
        self.assertEqual(self.git("rev-parse", "HEAD"), head_before)
        self.assertFalse((self.path / "feature.txt").exists())

    def test_conflicts_fail_instead_of_using_unmerged_diff(self):
        self.write("seed", "feature\n")
        self.commit()
        self.git("checkout", "-q", "develop")
        self.write("seed", "develop\n")
        self.commit()
        with self.assertRaises(MergeConflict):
            self.snapshot()

    def test_exclusions_are_provider_specific(self):
        self.write("generated/file.txt", "generated\n")
        self.write("src/file.txt", "source\n")
        self.commit()
        self.providers["qodo"] = ["generated/*"]
        snapshot = self.snapshot()
        self.assertEqual(snapshot.after["qodo"].files, 1)
        self.assertEqual(snapshot.after["coderabbit"].files, 2)

    def test_newline_tab_and_pathspec_characters_in_filename(self):
        self.write("odd\n\t[abc]*.txt", "source\n")
        self.commit()
        snapshot = self.snapshot()
        self.assertEqual(snapshot.after["qodo"].files, 1)
        self.assertGreater(snapshot.after["qodo"].patch_bytes, 0)

    def test_giant_single_line_is_not_small_review(self):
        self.write("minified.js", "x" * 20000)
        self.commit()
        volume = self.snapshot().after["qodo"]
        self.assertEqual(volume.lines, 1)
        self.assertGreater(volume.largest_patch_bytes, 20000)

    def test_binary_counts_actual_bytes_instead_of_tiny_diff_notice(self):
        (self.path / "binary").write_bytes(b"a\0b" * 10000)
        self.commit()
        volume = self.snapshot().after["qodo"]
        self.assertEqual(volume.files, 1)
        self.assertEqual(volume.lines, 0)
        self.assertGreaterEqual(volume.largest_patch_bytes, 30000)

    def test_divergent_main_does_not_force_rebase(self):
        self.write("feature.txt", "feature\n")
        self.commit()
        self.git("checkout", "-q", "main")
        self.write("hotfix.txt", "hotfix\n")
        self.commit()
        snapshot = self.snapshot()
        self.assertEqual(snapshot.after["qodo"].files, 1)

    def test_stale_admission_rejected_after_another_merge(self):
        self.write("feature.txt", "feature\n")
        self.commit()
        snapshot = self.snapshot()
        self.git("checkout", "-q", "develop")
        self.write("other.txt", "other\n")
        self.commit()
        with self.assertRaisesRegex(GitError, "changed"):
            self.repo.assert_current(snapshot, "main", "develop", "feature")

    def test_external_diff_driver_never_executed(self):
        marker = self.path / "executed"
        driver = self.path / "driver.sh"
        driver.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\n")
        driver.chmod(0o700)
        self.git("config", "diff.dangerous.command", str(driver))
        self.write(".gitattributes", "*.txt diff=dangerous\n")
        self.write("source.txt", "data\n")
        self.commit()
        self.snapshot()
        self.assertFalse(marker.exists())

    def test_external_merge_driver_rejected_before_execution(self):
        marker = self.path / "executed"
        self.git("config", "merge.dangerous.driver", "touch " + str(marker))
        self.write(".gitattributes", "seed merge=dangerous\n")
        self.write("seed", "feature\n")
        self.commit()
        self.git("checkout", "-q", "develop")
        self.write("seed", "integration\n")
        self.commit()
        with self.assertRaisesRegex(GitError, "external merge drivers"):
            self.snapshot()
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
