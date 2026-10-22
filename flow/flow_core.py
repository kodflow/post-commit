"""Pure admission rules for permissive integration and qualified promotion.

This module never calls GitHub, runs project code, or modifies a repository.
The controller must supply fresh state and serialize admission with merging.
"""

from dataclasses import asdict, dataclass
from typing import Mapping


@dataclass(frozen=True)
class Volume:
    files: int
    lines: int
    patch_bytes: int
    largest_patch_bytes: int

    def __post_init__(self):
        for key, value in asdict(self).items():
            if type(value) is not int or value < 0:
                raise ValueError(f"{key} must be a nonnegative integer")

    def exceeds(self, other):
        return [key for key, value in asdict(self).items()
                if value > getattr(other, key)]


@dataclass(frozen=True)
class Limits:
    soft: Volume
    hard: Volume

    def __post_init__(self):
        if self.soft.exceeds(self.hard):
            raise ValueError("soft limits must not exceed hard limits")
        if any(value == 0 for value in asdict(self.hard).values()):
            raise ValueError("hard limits must be positive")


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str
    violations: tuple[str, ...] = ()


def admit_integration(
    before: Mapping[str, Volume],
    after: Mapping[str, Volume],
    limits: Mapping[str, Limits],
    *,
    promotion_open: bool,
    correction_authorized: bool,
    conflicts: bool = False,
    governance_passed: bool = True,
) -> Verdict:
    """Lint/unit results intentionally do not enter the admission decision.

    A correction is authorized by trusted repository state, never by a title,
    a branch name or configuration supplied by the pull request itself.
    """
    if not governance_passed:
        return Verdict(False, "governance-failed")
    if conflicts:
        return Verdict(False, "merge-conflict")
    if not limits or set(before) != set(limits) or set(after) != set(limits):
        raise ValueError("every configured reviewer requires before/after metrics")
    if promotion_open and not correction_authorized:
        return Verdict(False, "promotion-frozen")

    existing_hard_excess = any(before[key].exceeds(limits[key].hard) for key in limits)
    if existing_hard_excess and any(after[key].exceeds(before[key]) for key in limits):
        return Verdict(False, "hard-limit-recovery-must-not-grow")

    # A legacy oversized develop can accept non-growing repairs, but cannot be
    # promoted until it is back below every hard limit. Equality permits fixes
    # which change content without changing review volume.
    hard_failures = []
    soft_failures = []
    recovering = False
    for reviewer, cap in limits.items():
        hard = after[reviewer].exceeds(cap.hard)
        if hard:
            if before[reviewer].exceeds(cap.hard) and not after[reviewer].exceeds(before[reviewer]):
                recovering = True
            else:
                hard_failures.extend(f"{reviewer}.{dimension}" for dimension in hard)
        soft_failures.extend(
            f"{reviewer}.{dimension}"
            for dimension in after[reviewer].exceeds(cap.soft)
        )
    if hard_failures:
        return Verdict(False, "hard-limit", tuple(hard_failures))
    if soft_failures and not correction_authorized:
        # Same-volume and reducing changes remain admissible even after the
        # feature budget is exhausted, except while a promotion is frozen.
        if any(after[key].exceeds(before[key]) for key in limits):
            return Verdict(False, "soft-limit", tuple(soft_failures))
    return Verdict(True, "recovering" if recovering else "within-budget")


@dataclass(frozen=True)
class Review:
    head: str
    base: str
    completed: bool
    complete_coverage: bool
    blocking_findings: int
    error: str = ""

    def __post_init__(self):
        if type(self.blocking_findings) is not int or self.blocking_findings < 0:
            raise ValueError("blocking_findings must be a nonnegative integer")
        if type(self.completed) is not bool or type(self.complete_coverage) is not bool:
            raise ValueError("review completion and coverage must be booleans")
        if not isinstance(self.error, str):
            raise ValueError("review error must be a string")


def admit_promotion(
    *,
    head: str,
    base: str,
    source_repository: str,
    repository: str,
    source_branch: str,
    volumes: Mapping[str, Volume],
    limits: Mapping[str, Limits],
    reviews: Mapping[str, Review],
    checks: Mapping[str, str],
    required_checks: tuple[str, ...],
    conflicts: bool = False,
    draft: bool = False,
) -> Verdict:
    """Checks are results for this exact head/base candidate, fetched by caller.

    Missing, neutral, skipped, pending and cancelled checks all fail closed.
    The controller supplies independent lint/unit and heavy check names.
    """
    if not head or not base or head == base:
        return Verdict(False, "invalid-candidate")
    if source_repository != repository or source_branch != "develop":
        return Verdict(False, "invalid-promotion-source")
    if conflicts or draft:
        return Verdict(False, "candidate-not-ready")
    if not limits or set(volumes) != set(limits) or set(reviews) != set(limits):
        return Verdict(False, "missing-review-evidence")
    violations = tuple(
        f"{reviewer}.{dimension}"
        for reviewer, cap in limits.items()
        for dimension in volumes[reviewer].exceeds(cap.hard)
    )
    if violations:
        return Verdict(False, "hard-limit", violations)
    if not required_checks or len(set(required_checks)) != len(required_checks):
        raise ValueError("required checks must be nonempty and unique")
    failures = tuple(name for name in required_checks if checks.get(name) != "success")
    if failures:
        return Verdict(False, "validation-incomplete", failures)
    for reviewer, review in reviews.items():
        if review.head != head or review.base != base:
            return Verdict(False, "stale-review", (reviewer,))
        if (review.error or not review.completed or not review.complete_coverage
                or review.blocking_findings):
            return Verdict(False, "review-incomplete", (reviewer,))
    return Verdict(True, "qualified")
