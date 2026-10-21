"""Measure the actual promotion diff without checking out or running PR code."""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from flow_core import Volume
from flow_paths import excluded


class GitError(RuntimeError):
    pass


class MergeConflict(GitError):
    pass


class UnreviewableBinary(GitError):
    pass


@dataclass(frozen=True)
class Snapshot:
    main: str
    develop: str
    head: str
    merged_tree: str
    before: dict[str, Volume]
    after: dict[str, Volume]


class Repository:
    def __init__(self, path):
        self.path = Path(path)

    def git(self, *args, allowed=(0,), input_bytes=None):
        # Neither attributes-based external diff drivers nor user diff commands
        # may execute. Merge-tree writes only Git objects, never the worktree.
        env = os.environ.copy()
        env.pop("GIT_EXTERNAL_DIFF", None)
        for key in list(env):
            if key in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                       "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_COUNT"} or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
                env.pop(key)
        env["GIT_NO_REPLACE_OBJECTS"] = "1"
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        env["GIT_CONFIG_SYSTEM"] = "/dev/null"
        env.update({"GIT_AUTHOR_NAME": "Flow simulation", "GIT_COMMITTER_NAME": "Flow simulation",
                    "GIT_AUTHOR_EMAIL": "flow@example.invalid", "GIT_COMMITTER_EMAIL": "flow@example.invalid"})
        process = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.path), *args],
            env=env, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if process.returncode not in allowed:
            # Do not print diffs or file contents into public workflow logs.
            raise GitError(f"git {args[0]} failed with exit {process.returncode}")
        return process

    def commit(self, ref):
        value = self.git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
        sha = value.stdout.decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise GitError("invalid commit object")
        return sha

    def merge_tree(self, base, head):
        # A PR can choose a merge driver through .gitattributes. Never let it
        # select an executable configured by the runner or target repository.
        # Built-in text/binary/union drivers require no merge.*.driver setting.
        drivers = self.git("config", "--local", "--get-regexp", r"^merge\..*\.driver$", allowed=(0, 1))
        if drivers.returncode == 0 and drivers.stdout.strip():
            raise GitError("external merge drivers are forbidden in the simulation repository")
        result = self.git("merge-tree", "--write-tree", base, head, allowed=(0, 1))
        if result.returncode:
            raise MergeConflict("candidate conflicts with the integration branch")
        tree = result.stdout.splitlines()[0].decode("ascii")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tree):
            raise GitError("invalid merged tree")
        return tree

    def metrics(self, main, tree, excludes=()):
        """Tree-to-tree patch volume, including context and deletions.

        Renames are deliberately measured as delete+add: conservative and not
        sensitive to heuristic rename detection thresholds. Exclusions must
        come from the controller's trusted, versioned policy.
        """
        args = ("diff", "--no-ext-diff", "--no-textconv", "--no-renames")
        output = self.git(*args, "--numstat", "-z", main, tree, "--").stdout
        files = lines = patch_bytes = largest = 0
        for record in output.split(b"\0"):
            if not record:
                continue
            added, deleted, filename = record.split(b"\t", 2)
            path = os.fsdecode(filename)
            if excluded(path, excludes):
                continue
            patch = self.git(*args, "--unified=3", "--no-color", main, tree,
                             "--", ":(literal)" + path).stdout
            if added == b"-" or deleted == b"-":
                # Git's binary diff notice is tiny even for a huge image.
                # Count actual before+after blob bytes instead of hiding size.
                binary_size = 0
                for revision in (main, tree):
                    size = self.git("cat-file", "-s", revision + ":" + path, allowed=(0, 128))
                    if size.returncode == 0:
                        binary_size += int(size.stdout)
                size_bytes = max(len(patch), binary_size)
                changed_lines = 0
            else:
                size_bytes = len(patch)
                changed_lines = int(added) + int(deleted)
            files += 1
            lines += changed_lines
            patch_bytes += size_bytes
            largest = max(largest, size_bytes)
        return Volume(files, lines, patch_bytes, largest)

    def simulate(self, main, develop, head, providers):
        if not providers:
            raise ValueError("at least one reviewer policy is required")
        if any(not isinstance(patterns, (tuple, list)) or
               any(not isinstance(pattern, str) or not pattern for pattern in patterns)
               for patterns in providers.values()):
            raise ValueError("review exclusions must be lists of nonempty glob patterns")
        main, develop, head = (self.commit(ref) for ref in (main, develop, head))
        merged = self.merge_tree(develop, head)
        # Compute the actual PR merge-base on both sides of the proposed merge.
        # A synthetic commit preserves the ancestry of a feature which already
        # contains a main hotfix. No rebase or base update is required here.
        synthetic = self.git("-c", "commit.gpgsign=false", "commit-tree", merged,
                             "-p", develop, "-p", head,
                             input_bytes=b"flow: simulate admission\n").stdout.decode().strip()
        before_base = self.git("merge-base", main, develop).stdout.decode().strip()
        after_base = self.git("merge-base", main, synthetic).stdout.decode().strip()
        before = {key: self.metrics(before_base, develop, patterns) for key, patterns in providers.items()}
        after = {key: self.metrics(after_base, merged, patterns) for key, patterns in providers.items()}
        return Snapshot(main, develop, head, merged, before, after)

    def assert_current(self, snapshot, main, develop, head):
        observed = tuple(self.commit(ref) for ref in (main, develop, head))
        if observed != (snapshot.main, snapshot.develop, snapshot.head):
            raise GitError("candidate or base changed: admission must be recalculated")
