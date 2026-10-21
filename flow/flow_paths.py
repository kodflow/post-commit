"""POSIX glob matching: * stays within a path segment, ** crosses directories."""

import fnmatch
from functools import lru_cache


def matches(path, pattern):
    segments = path.split("/")
    globs = pattern.lstrip("/").split("/")
    @lru_cache(None)
    def walk(i, j):
        if j == len(globs):
            return i == len(segments)
        if globs[j] == "**":
            return walk(i, j + 1) or (i < len(segments) and walk(i + 1, j))
        return i < len(segments) and fnmatch.fnmatchcase(segments[i], globs[j]) and walk(i + 1, j + 1)
    return walk(0, 0)


def excluded(path, patterns):
    return any(matches(path, pattern) for pattern in patterns)
