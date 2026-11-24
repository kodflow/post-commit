#!/usr/bin/env python3
"""CodeRabbit metrics per owner x PR author x repository (stdlib, Python 3.9+).

Reads the pull requests opened in a window through `gh api graphql` and writes a
markdown report. Only ELIGIBLE pull requests are counted: the author
kodflow-ci[bot] and the five sync titles (case-insensitive substring) are
excluded and counted apart, because stub/coderabbit/central.yaml skips them on
purpose (tests/coderabbit_org_test.py holds the two lists below to that file).

    python3 scripts/coderabbit_metrics.py [--since YYYY-MM-DD] [--until YYYY-MM-DD]
                                          [--owner OWNER ...] [--out-dir DIR | --stdout]

The report names repositories, private ones included: it is written under
build/coderabbit/reports/ (ignored by git) and is never committed to this
public repository. Default window: the last 7 days ending today. Limits of the reading: a comment
CodeRabbit edits keeps only its last state (a refusal later replaced by a
review is not seen), and each PR is read up to 100 comments and 100 reviews.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

OWNERS = ("kodflow", "kitsunium", "supervizio")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "build", "coderabbit", "reports")
CR_LOGIN = "coderabbitai"
EXCLUDED_AUTHORS = ("kodflow-ci",)
EXCLUDED_TITLES = (
    "ci(post-commit): sync the gate workflow with the central stub",
    "ci(runner-template): sync the stubs with kodflow/post-commit",
    "ci: add the mandatory post-commit gate",
    "ci: run every job on the self-hosted kodflow-runner",
    "ci(coderabbit): sync .coderabbit.yaml with the central template",
)

QUERY = """query($q:String!,$c:String){ search(query:$q, type:ISSUE, first:50, after:$c){
 issueCount pageInfo{hasNextPage endCursor}
 nodes{ ... on PullRequest{ number title createdAt state mergedAt headRefOid
  repository{nameWithOwner} author{login __typename}
  comments(first:100){nodes{author{login} body}}
  reviews(first:100){nodes{author{login} body}}
 }}}}"""

RE_COMPLETED = re.compile(r"Actionable comments posted|No actionable comments were generated")
RE_RATE_LIMIT = re.compile(r"rate limited by coderabbit\.ai|Review limit reached")
RE_PAUSE = re.compile(r"review paused by coderabbit\.ai")
RE_COVERED = re.compile(r'final_review_risk_coverage:\s*(\{.*?\})\s*-->')
RE_LAST_COMMIT = re.compile(r"Last reviewed commit:\s*([0-9a-f]{7,40})")
RE_PLAN = re.compile(r"\*\*Plan\*\*:\s*([A-Za-z][\w ]*?)\s*$", re.M)
# The two known forms of the allowance sentence. Both end on a rate per hour
# that is also captured on its own; any other sentence counts as `unparsed`.
RE_ALLOW_PLAN = re.compile(r"Your plan provides up to (\d+) included reviews? per hour[^\n.]*\.?")
RE_ALLOW_ATTEMPTS = re.compile(
    r"PR review attempts over the past 7 days set your current allowance at (\d+) reviews? per hour\.?")
RE_ALLOW_LINE = re.compile(r"\*\*Included review availability:\*\*[^\n]*")


def norm_login(login):
    return (login or "").lower().removesuffix("[bot]")


def is_excluded(author, title):
    if norm_login(author) in EXCLUDED_AUTHORS:
        return True
    low = (title or "").lower()
    return any(t.lower() in low for t in EXCLUDED_TITLES)


def last_reviewed(bodies):
    """Last commit CodeRabbit covered: the summary markers win over the text."""
    marker = text = None
    for body in bodies:
        for m in RE_COVERED.finditer(body):
            try:
                marker = json.loads(m.group(1)).get("coveredCommitId") or marker
            except ValueError:
                pass
        m = RE_LAST_COMMIT.search(body)
        if m:
            text = m.group(1)
    return marker or text


def parse_allowance(body):
    """(plan, rate_per_hour, raw_sentence, parsed) from one CodeRabbit body."""
    plan = None
    m = RE_PLAN.search(body)
    if m:
        plan = m.group(1)
    for rx in (RE_ALLOW_PLAN, RE_ALLOW_ATTEMPTS):
        m = rx.search(body)
        if m:
            line = RE_ALLOW_LINE.search(body)
            return plan, int(m.group(1)), (line.group(0) if line else m.group(0)).strip(), True
    line = RE_ALLOW_LINE.search(body)
    if line:
        return plan, None, line.group(0).strip(), False
    return plan, None, None, True


def analyse(pr):
    cr = [c["body"] or "" for c in pr["comments"]["nodes"] + pr["reviews"]["nodes"]
          if norm_login((c["author"] or {}).get("login")) == CR_LOGIN]
    merged = pr["state"] == "MERGED"
    last = last_reviewed(cr)
    covered = None
    if merged:
        head = pr["headRefOid"] or ""
        covered = "unknown" if not last else ("yes" if head.startswith(last) or last.startswith(head) else "no")
    plan = rate = raw = None
    unparsed = 0
    for body in cr:
        p, r, s, ok = parse_allowance(body)
        plan = p or plan
        if s:
            rate, raw = (r if r is not None else rate), s
        if not ok:
            unparsed += 1
    return dict(
        reviewed=any(RE_COMPLETED.search(b) for b in cr),
        refusals=sum(1 for b in cr if RE_RATE_LIMIT.search(b)),
        pauses=sum(1 for b in cr if RE_PAUSE.search(b)),
        merged=merged, covered=covered, plan=plan, rate=rate, raw=raw, unparsed=unparsed)


def gh_search(query):
    nodes, cursor = [], None
    while True:
        args = ["gh", "api", "graphql", "-f", "query=" + QUERY, "-f", "q=" + query]
        if cursor:
            args += ["-f", "c=" + cursor]
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError((r.stderr or r.stdout).strip()[:300])
        s = json.loads(r.stdout)["data"]["search"]
        nodes += [n for n in s["nodes"] if n]
        if s["issueCount"] > 1000:
            raise RuntimeError("more than 1000 pull requests in the window: narrow --since/--until")
        if not s["pageInfo"]["hasNextPage"]:
            return nodes
        cursor = s["pageInfo"]["endCursor"]


def report_owner(owner, since, until, out):
    prs = gh_search(f"org:{owner} is:pr created:{since}..{until}")
    excluded = 0
    groups = defaultdict(lambda: defaultdict(int))
    meta = defaultdict(dict)
    plans = set()
    for pr in prs:
        author = (pr["author"] or {}).get("login", "ghost")
        if is_excluded(author, pr["title"]):
            excluded += 1
            continue
        a = analyse(pr)
        key = (author, pr["repository"]["nameWithOwner"])
        g = groups[key]
        g["opened"] += 1
        g["reviewed"] += a["reviewed"]
        g["refusals"] += a["refusals"]
        g["pauses"] += a["pauses"]
        g["merged"] += a["merged"]
        g["not_covered"] += a["covered"] == "no"
        g["unknown"] += a["covered"] == "unknown"
        g["unparsed"] += a["unparsed"]
        if a["plan"]:
            plans.add(a["plan"])
            meta[key]["plan"] = a["plan"]
        if a["raw"]:
            meta[key]["raw"] = a["raw"]
    out.append(f"## {owner}\n")
    out.append(f"Eligible PRs: {sum(g['opened'] for g in groups.values())}; "
               f"excluded (kodflow-ci[bot] or sync title): {excluded}; "
               f"plan seen: {', '.join(sorted(plans)) or 'none'}\n")
    out.append("| Author | Repo | Opened | >=1 review | Refusals | Pauses | Merged | Merged, head not reviewed | Unknown | Unknown rate | Plan | Allowance sentence | Unparsed |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for (author, repo), g in sorted(groups.items()):
        unk = f"{100 * g['unknown'] // g['merged']} %" if g["merged"] else "-"
        m = meta[(author, repo)]
        raw = (m.get("raw") or "-").replace("|", "/")
        out.append(f"| {author} | {repo} | {g['opened']} | {g['reviewed']} | {g['refusals']} | {g['pauses']} "
                   f"| {g['merged']} | {g['not_covered']} | {g['unknown']} | {unk} | {m.get('plan', '-')} | {raw} | {g['unparsed']} |")
    out.append("")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    today = datetime.date.today()
    ap.add_argument("--since", default=(today - datetime.timedelta(days=7)).isoformat())
    ap.add_argument("--until", default=today.isoformat())
    ap.add_argument("--owner", action="append", default=None)
    dest = ap.add_mutually_exclusive_group()
    dest.add_argument("--out-dir", default=OUT_DIR,
                      help="where the report is written (default: build/coderabbit/reports/, git-ignored)")
    dest.add_argument("--stdout", action="store_true", help="print the report instead of writing it")
    args = ap.parse_args(argv)
    out = [f"# CodeRabbit metrics, {args.since} to {args.until}\n",
           "Generated by `scripts/coderabbit_metrics.py`. Eligible PRs only; "
           "a refusal or pause is counted as the comment stands at read time.\n"]
    failed = False
    for owner in args.owner or OWNERS:
        try:
            report_owner(owner, args.since, args.until, out)
        except Exception as e:  # an owner without access is recorded, never dropped
            failed = True
            out.append(f"## {owner}\n\nERROR: {e}\n")
    text = "\n".join(out) + "\n"
    if args.stdout:
        sys.stdout.write(text)
    else:
        os.makedirs(args.out_dir, exist_ok=True)
        path = os.path.join(args.out_dir, f"metrics-{args.since}_{args.until}.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(path)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
