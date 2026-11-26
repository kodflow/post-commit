"""Conservative adapters for provider-published review evidence.

Unknown output formats stay pending. Never infer completion from a green
status alone. File coverage means the provider explicitly listed the files;
it does not claim that a model discovered every defect.
"""

import re

from flow_core import Review
from flow_evidence import PROVIDERS, provider_blockers, timestamp


PARTIAL = re.compile(r"(?i)(files? (?:not processed|skipped|omitted)|partial review|review (?:failed|cancelled)|token limit exceeded)")


def listed_paths(body, expected):
    # Exact quoted paths avoid accepting 'foo.py' as a substring of 'old/foo.py'.
    quoted = set(re.findall(r"`([^`\n]+)`", body))
    return set(expected) <= quoted


def current_qodo_section(body):
    return re.split(
        r"(?im)(?:<summary>[^<]*(?:previous reviews|audit trail)|^\s*#{1,6}\s+previous reviews\b)",
        body, maxsplit=1,
    )[0]


def evidence(provider, *, head, base, anchor_time, expected_paths, reviews, comments, statuses, unresolved):
    if unresolved:
        return Review(head, base, False, False, len(unresolved), "unresolved-findings")
    authors = PROVIDERS[provider]["authors"]
    cutoff = timestamp(anchor_time)
    candidates = []
    for item in reviews:
        if (item.get("user", {}).get("login") in authors and item.get("commit_id") == head
                and item.get("submitted_at") and timestamp(item["submitted_at"]) >= cutoff):
            candidates.append((timestamp(item["submitted_at"]), item.get("body", ""), item["state"]))
    # Qodo documents a comment linking 'Review updated until commit'. A plain
    # summary without that head binding must not stand in for a review.
    if provider == "qodo":
        for item in comments:
            body = current_qodo_section(item.get("body", ""))
            identity = re.search(r"(?is)review updated until commit.{0,500}", body)
            if (item.get("user", {}).get("login") in authors and identity
                    and re.search(r"(?<![0-9a-f])" + re.escape(head) + r"(?![0-9a-f])", identity[0]) and item.get("updated_at")
                    and timestamp(item["updated_at"]) >= cutoff):
                candidates.append((timestamp(item["updated_at"]), body, "COMMENTED"))
    if not candidates:
        blockers = provider_blockers(provider, comments, statuses)
        return Review(head, base, False, False, 0, ",".join(blockers) or "review-not-completed")
    reviewed_at, body, state = max(candidates, key=lambda item: item[0])
    blockers = provider_blockers(provider, comments, statuses, reviewed_at=reviewed_at.isoformat())
    if blockers or PARTIAL.search(body):
        return Review(head, base, False, False, 0, ",".join(blockers) or "partial-review")
    if state == "CHANGES_REQUESTED":
        return Review(head, base, True, False, 1, "changes-requested")
    if state not in {"APPROVED", "COMMENTED"}:
        return Review(head, base, False, False, 0, "review-not-completed")
    if provider == "coderabbit":
        findings = re.search(r"Actionable comments posted:\s*([0-9]+)", body)
        selection = re.search(r"(?is)<summary>[^<]*Files selected for processing:\s*([0-9]+)</summary>(.*?)</details>", body)
        if not findings or not selection:
            return Review(head, base, False, False, 0, "unknown-review-format")
        # A selected-file count that disagrees with its list is not complete.
        selected = set(re.findall(r"(?m)^\s*[-*] `([^`\n]+)`\s*$", selection[2]))
        covered = set(expected_paths) <= selected and int(selection[1]) == len(selected)
        return Review(head, base, True, covered, int(findings[1]))
    current = current_qodo_section(body)
    no_findings = re.search(r"(?im)^\s*[*_✅ ]*no (?:issues|findings|problems) (?:were )?(?:found|detected|identified)[*_!. ]*\s*$", current)
    if not no_findings:
        return Review(head, base, False, False, 0, "findings-not-cleared")
    return Review(head, base, True, listed_paths(current, expected_paths), 0)
