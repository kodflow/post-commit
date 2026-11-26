"""Reject provider failures which can hide behind a green commit status.

This only extracts negative evidence. It NEVER infers a completed review from
a successful status, a comment, or the absence of findings. Positive evidence
must separately identify the reviewed head/base and complete coverage.
"""

from datetime import datetime, timezone


PROVIDERS = {
    "qodo": {
        "authors": {"qodo-code-review[bot]", "qodo-merge-pro[bot]", "pr-agent[bot]"},
        "contexts": {"Qodo", "Qodo Code Review", "PR-Agent"},
        "markers": {"qodo:billing-blocked": "billing-blocked",
                    "out of credits": "billing-blocked"},
    },
    "coderabbit": {
        "authors": {"coderabbitai[bot]"},
        "contexts": {"CodeRabbit"},
        "markers": {"rate limited by coderabbit.ai": "rate-limited",
                    "review rate limited": "rate-limited",
                    "review limit reached": "rate-limited"},
    },
}


def timestamp(value):
    if not isinstance(value, str) or not value:
        raise ValueError("evidence must have a UTC timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("evidence timestamps must include a timezone")
    return result.astimezone(timezone.utc)


def provider_blockers(provider, comments, statuses, *, reviewed_at=None):
    """statuses must already be scoped to the candidate head by the API caller.

    The optional cutoff is only supplied after independently verifying a
    completed review for this exact candidate. It prevents an old billing
    notice from vetoing a genuinely newer review forever.
    """
    definition = PROVIDERS[provider]
    cutoff = timestamp(reviewed_at) if reviewed_at else None
    found = set()
    candidates = []
    for comment in comments:
        if comment.get("user", {}).get("login") not in definition["authors"]:
            continue
        candidates.append((comment.get("body", ""), comment.get("updated_at")))
    for status in statuses:
        if status.get("context") not in definition["contexts"]:
            continue
        # Deliberately inspect the description even when state == success.
        candidates.append((status.get("description") or "", status.get("updated_at")))
    for body, updated_at in candidates:
        matched = {reason for marker, reason in definition["markers"].items()
                   if marker in body.lower()}
        if not matched:
            continue
        if cutoff:
            # An undated blocking notice cannot be proved older than a review.
            if not updated_at:
                found.add("unverifiable-timestamp")
                continue
            if timestamp(updated_at) < cutoff:
                continue
        found.update(matched)
    return tuple(sorted(found))
