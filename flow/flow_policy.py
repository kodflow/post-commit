"""Trusted policy parsing and GitHub branch/org ruleset payloads.

API schema checked against https://docs.github.com/en/rest/repos/rules and
https://docs.github.com/en/rest/orgs/rules on 2026-10-21.
"""

import json
import re
from pathlib import Path

from flow_core import Limits, Volume


METRICS = {"files", "lines", "patch_bytes", "largest_patch_bytes"}
REVIEWERS = {"qodo", "coderabbit"}


def positive_integer(value, label):
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def load_policy(path):
    return validate_policy(json.loads(Path(path).read_text()))


def validate_policy(policy):
    if type(policy.get("version")) is not int or policy.get("version") != 1:
        raise ValueError("policy version must be 1")
    if set(policy.get("reviewers", {})) != REVIEWERS:
        raise ValueError("qodo and coderabbit reviewer policies are required")
    for name, reviewer in policy["reviewers"].items():
        if set(reviewer) != {"soft", "hard", "exclude"}:
            raise ValueError(f"{name}: expected soft, hard and exclude")
        for boundary in ("soft", "hard"):
            if set(reviewer[boundary]) != METRICS:
                raise ValueError(f"{name}.{boundary}: incomplete review dimensions")
        Limits(Volume(**reviewer["soft"]), Volume(**reviewer["hard"]))
        excludes = reviewer["exclude"]
        if not isinstance(excludes, list) or any(not isinstance(p, str) or not p for p in excludes):
            raise ValueError(f"{name}: excludes must be a list of glob patterns")
        if any(p in ("*", "**", "**/*") for p in excludes):
            raise ValueError(f"{name}: excluding the entire repository is forbidden")
    checks = policy.get("promotion_checks")
    if (not isinstance(checks, list) or not checks
            or any(not isinstance(c, str) or not c.strip() for c in checks)
            or len(checks) != len(set(checks))):
        raise ValueError("promotion_checks must be a nonempty unique list")
    if not {"ci-lint", "ci-unit", "ci-heavy"}.issubset(checks):
        raise ValueError("promotion requires ci-lint, ci-unit and ci-heavy")
    for kind, context in policy.get("check_sources", {}).items():
        if kind not in checks:
            raise ValueError(f"unknown check source: {kind}")
        positive_integer(context, f"{kind}.app_id")
    if set(policy.get("check_sources", {})) != set(checks):
        raise ValueError("every promotion check requires its expected GitHub App ID")
    positive_integer(policy.get("gate_app_id"), "gate_app_id")
    positive_integer(policy.get("merger_app_id"), "merger_app_id")
    return policy


def limits(policy):
    return {name: Limits(Volume(**item["soft"]), Volume(**item["hard"]))
            for name, item in policy["reviewers"].items()}


def required_checks(contexts, app_ids):
    return {"type": "required_status_checks", "parameters": {
        # develop is permissive: updating the branch merely because the base
        # advanced is never a requirement. The sole merger recalculates budget.
        "strict_required_status_checks_policy": False,
        "do_not_enforce_on_create": False,
        "required_status_checks": [
            {"context": name, "integration_id": app_ids[name]} for name in contexts
        ],
    }}


def rulesets(policy, repository_names=None):
    """Separate writer restriction from checks so the merger cannot bypass CI.

    repository_names=None yields repository rulesets. An explicit nonempty
    list scopes organization rulesets to migrated repositories only.
    """
    if repository_names is not None:
        if not repository_names or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", n)
                                       for n in repository_names):
            raise ValueError("organization rules require explicit migrated repository names")
    def make(name, branches, rules, bypass=()):
        conditions = {"ref_name": {"include": ["refs/heads/" + b for b in branches], "exclude": []}}
        if repository_names is not None:
            conditions["repository_name"] = {
                "include": sorted(set(repository_names)), "exclude": [], "protected": False,
            }
        return {"name": name, "target": "branch", "enforcement": "active",
                "bypass_actors": list(bypass), "conditions": conditions, "rules": rules}

    common = [{"type": "deletion"}, {"type": "non_fast_forward"},
              {"type": "pull_request", "parameters": {
                  "required_approving_review_count": 0,
                  "dismiss_stale_reviews_on_push": False,
                  "require_code_owner_review": False,
                  "require_last_push_approval": False,
                  "required_review_thread_resolution": False,
              }}]
    gate_app = policy["gate_app_id"]
    develop_contexts = ("post-commit", "flow-admission")
    main_contexts = ("post-commit", "flow-promotion", *policy["promotion_checks"])
    return [
        make("flow-develop", ["develop"], common + [required_checks(
            develop_contexts, {"post-commit": 15368, "flow-admission": gate_app})]),
        make("flow-main", ["main"], common + [required_checks(
            main_contexts, {"post-commit": 15368, "flow-promotion": gate_app,
                            **policy["check_sources"]})]),
        make("flow-writer", ["develop", "main"], [{"type": "update"}], [{
            "actor_type": "Integration", "actor_id": policy["merger_app_id"],
            "bypass_mode": "pull_request",
        }]),
    ]
