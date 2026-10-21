"""Serialized GitHub admission controller. Default mode is read-only.

Run once per repository through the pinned flow action and its concurrency
group. Only this App may update develop/main; other rules still apply to it.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import zipfile
from dataclasses import asdict
from pathlib import Path

from flow_api import APIError, GitHub, authorized_label, repository_name
from flow_core import Verdict, admit_integration, admit_promotion
from flow_evidence import PROVIDERS
from flow_git import GitError, Repository
from flow_policy import limits, load_policy
from flow_paths import excluded
from flow_reviews import evidence


def blob_sha(content):
    if isinstance(content, str):
        content = content.encode()
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


def contents(api, repo, path, ref):
    result = api.request("GET", f"repos/{repo}/contents/{path}?ref={ref}")
    if result.get("encoding") != "base64" or result.get("size", 0) > 1024 * 1024:
        raise ValueError("managed configuration must be a small base64 text blob")
    data = base64.b64decode(result["content"])
    if blob_sha(data) != result["sha"]:
        raise ValueError("configuration blob checksum mismatch")
    return data, result["sha"]


def checkout_objects(path, repo, number, token):
    env = os.environ.copy()
    env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1"})
    if token:
        encoded = base64.b64encode(("x-access-token:" + token).encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + encoded})
    def git(*args):
        process = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(path), *args],
                                 env=env, capture_output=True, timeout=300, check=False)
        if process.returncode:
            raise GitError("could not fetch candidate objects")
    git("init", "--bare", "-q")
    git("fetch", "--quiet", "--no-tags", "https://github.com/" + repo + ".git",
        "+refs/heads/main:refs/heads/main", "+refs/heads/develop:refs/heads/develop",
        f"+refs/pull/{number}/head:refs/heads/candidate")


def unresolved_threads(api, repo, number):
    owner, name = repo.split("/")
    query = """query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$cursor){nodes{id isResolved isOutdated comments(first:1){nodes{author{login}}}} pageInfo{hasNextPage endCursor}}}}}"""
    cursor = None
    threads = {provider: [] for provider in PROVIDERS}
    for _ in range(1000):
        response = api.request("POST", "graphql", {"query": query, "variables": {
            "owner": owner, "name": name, "number": number, "cursor": cursor}})
        if response.get("errors"):
            raise ValueError("could not read review threads")
        connection = response["data"]["repository"]["pullRequest"]["reviewThreads"]
        for thread in connection["nodes"]:
            if thread["isResolved"] or thread["isOutdated"]:
                continue
            author = thread["comments"]["nodes"][0].get("author") or {}
            for provider, definition in PROVIDERS.items():
                if author.get("login") in definition["authors"]:
                    threads[provider].append(thread["id"])
        if not connection["pageInfo"]["hasNextPage"]:
            return threads
        cursor = connection["pageInfo"]["endCursor"]
    raise ValueError("review threads pagination limit reached")


def validation_receipt(api, repo, pr, policy):
    """Pair current PR SHA/base with immutable metadata from the selected CI run."""
    workflow = policy["qualification_workflow"]
    runs = api.pages(f"repos/{repo}/actions/runs", "workflow_runs", {
        "head_sha": pr["head"]["sha"], "event": "pull_request"})
    runs = [run for run in runs if run.get("path", "").split("@")[0] == workflow]
    if not runs:
        return {}, "validation-not-started"
    run = max(runs, key=lambda item: item["id"])
    if run["status"] != "completed":
        return {}, "validation-running"
    attempt = run.get("run_attempt", 1)
    artifacts = api.pages(f"repos/{repo}/actions/runs/{run['id']}/artifacts", "artifacts")
    artifacts = [a for a in artifacts if a["name"] == f"flow-candidate-{attempt}" and not a.get("expired")]
    if len(artifacts) != 1 or artifacts[0].get("size_in_bytes", 0) > 65536:
        return {}, "candidate-receipt-missing"
    raw = api.request("GET", f"repos/{repo}/actions/artifacts/{artifacts[0]['id']}/zip", raw=True)
    if len(raw) > 65536:
        raise ValueError("oversized candidate receipt")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if archive.namelist() != ["flow-candidate.json"] or archive.infolist()[0].file_size > 4096:
            raise ValueError("invalid candidate receipt archive")
        receipt = json.loads(archive.read("flow-candidate.json"))
    expected = {"version": 1, "pr": pr["number"], "head": pr["head"]["sha"],
                "base": pr["base"]["sha"], "merge": pr["merge_commit_sha"],
                "run_id": run["id"], "attempt": attempt}
    if receipt != expected:
        return {}, "stale-validation"
    jobs = api.pages(f"repos/{repo}/actions/runs/{run['id']}/attempts/{attempt}/jobs", "jobs")
    result = {}
    for context in policy["promotion_checks"]:
        matches = [job for job in jobs if job["name"] == context]
        # A missing or duplicate summary must not accidentally validate a lane.
        if len(matches) == 1 and matches[0]["status"] == "completed":
            result[context] = matches[0]["conclusion"]
    return result, ""


class Controller:
    def __init__(self, api, repo, *, apply=False, token=None):
        self.api, self.repo, self.apply = api, repository_name(repo), apply
        self.token = token
        self.status_cache = {}
        self.bot_login = "kodflow-ci[bot]"

    def status(self, sha, context, state, reason):
        if not self.apply:
            return None
        if sha not in self.status_cache:
            self.status_cache[sha] = self.api.pages(f"repos/{self.repo}/commits/{sha}/statuses")
        for previous in self.status_cache[sha]:
            if previous["context"] == context and (previous.get("creator") or {}).get("login") == self.bot_login:
                if previous["state"] == state and previous["description"] == reason[:140]:
                    return previous
                break
        result = self.api.request("POST", f"repos/{self.repo}/statuses/{sha}", {
            "context": context, "state": state, "description": reason[:140],
        })
        self.status_cache[sha].insert(0, result)
        return result

    def fresh(self, pr, main, develop):
        current = self.api.request("GET", f"repos/{self.repo}/pulls/{pr['number']}")
        refs = {branch: self.api.request("GET", f"repos/{self.repo}/git/ref/heads/{branch}")["object"]["sha"]
                for branch in ("main", "develop")}
        if not authorized_label(self.api, self.repo, current, "flow:merge"):
            return False
        if current["base"]["ref"] != pr["base"]["ref"]:
            return False
        if current["base"]["ref"] == "develop":
            opened = self.api.pages(f"repos/{self.repo}/pulls", parameters={"state": "open"})
            frozen = any(p["base"]["ref"] == "main" and p["head"]["ref"] == "develop"
                         and (p["head"].get("repo") or {}).get("full_name") == self.repo and not p["draft"] for p in opened)
            if frozen and not authorized_label(self.api, self.repo, current, "flow:correction"):
                return False
        return (current["state"] == "open" and not current["draft"]
                and current["head"]["sha"] == pr["head"]["sha"]
                and refs == {"main": main, "develop": develop})

    def writer_locked(self, policy):
        # A PR check alone cannot serialize merges. Verify the actual update
        # restriction, including inherited org rules, before the controller merges.
        for branch in ("main", "develop"):
            rules = self.api.pages(f"repos/{self.repo}/rules/branches/{branch}")
            restrictions = [r for r in rules if r["type"] == "update"]
            valid = False
            for rule in restrictions:
                source = rule.get("ruleset_source_type")
                endpoint = (f"orgs/{self.repo.split('/')[0]}" if source == "Organization" else f"repos/{self.repo}")
                detail = self.api.request("GET", endpoint + f"/rulesets/{rule['ruleset_id']}")
                expected = [{"actor_type": "Integration", "actor_id": policy["merger_app_id"], "bypass_mode": "pull_request"}]
                if (detail.get("name") == "flow-writer" and detail.get("enforcement") == "active"
                        and detail.get("bypass_actors") == expected
                        and detail.get("rules") == [{"type": "update"}]):
                    valid = True
            if not valid:
                return False
        return True

    def anchor(self, pr, statuses):
        description = "base=" + pr["base"]["sha"]
        for item in statuses:
            if (item["context"] == "flow-anchor" and item["description"] == description
                    and (item.get("creator") or {}).get("login") == self.bot_login):
                return item["created_at"]
        item = self.status(pr["head"]["sha"], "flow-anchor", "success", description)
        return item["created_at"] if item else None

    def request_reviews(self, pr, comments):
        if not self.apply:
            return
        for provider, command in (("coderabbit", "@coderabbitai full review"), ("qodo", "/agentic_review")):
            marker = f"<!-- flow-review-request {provider} head={pr['head']['sha']} base={pr['base']['sha']} -->"
            if any((comment.get("user") or {}).get("login") == self.bot_login and marker in comment.get("body", "") for comment in comments):
                continue
            self.api.request("POST", f"repos/{self.repo}/issues/{pr['number']}/comments", {
                "body": command + "\n\n" + marker})

    def evaluate(self, pr, open_prs, policy):
        context = "flow-promotion" if pr["base"]["ref"] == "main" else "flow-admission"
        head = pr["head"]["sha"]
        if pr["draft"] or pr.get("mergeable") is not True:
            return context, Verdict(False, "draft-conflicting-or-mergeability-pending"), None
        data, policy_blob = contents(self.api, self.repo, ".github/flow-policy.json", "main")
        # Read the policy from the protected base branch, never the PR head.
        if json.loads(data) != policy:
            raise ValueError("policy changed during evaluation")
        maintenance = authorized_label(self.api, self.repo, pr, "flow:policy", admin_only=True)
        for path, expected in {**policy["managed_files"], ".github/flow-policy.json": policy_blob}.items():
            try:
                _, actual = contents(self.api, self.repo, path, head)
            except APIError:
                return context, Verdict(False, "managed-policy-file-missing", (path,)), None
            if actual != expected and not maintenance:
                return context, Verdict(False, "managed-policy-file-changed", (path,)), None
        with tempfile.TemporaryDirectory(prefix="flow-candidate-") as directory:
            checkout_objects(directory, self.repo, pr["number"], self.token)
            git = Repository(directory)
            main, develop = git.commit("main"), git.commit("develop")
            if pr["base"]["sha"] != (main if pr["base"]["ref"] == "main" else develop):
                return context, Verdict(False, "base-changed"), None
            if git.commit("candidate") != head:
                return context, Verdict(False, "candidate-changed"), None
            providers = {name: item["exclude"] for name, item in policy["reviewers"].items()}
            caps = limits(policy)
            if pr["base"]["ref"] == "develop":
                frozen = any(p["base"]["ref"] == "main" and p["head"]["ref"] == "develop"
                             and (p["head"].get("repo") or {}).get("full_name") == self.repo and not p["draft"] for p in open_prs)
                snapshot = git.simulate(main, develop, head, providers)
                verdict = admit_integration(snapshot.before, snapshot.after, caps,
                    promotion_open=frozen, correction_authorized=authorized_label(self.api, self.repo, pr, "flow:correction"))
            else:
                if pr["head"]["ref"] != "develop" or (pr["head"].get("repo") or {}).get("full_name") != self.repo or head != develop:
                    return context, Verdict(False, "invalid-promotion-source"), None
                statuses = self.api.pages(f"repos/{self.repo}/commits/{head}/statuses")
                merge_base = git.git("merge-base", main, head).stdout.decode().strip()
                volumes = {name: git.metrics(merge_base, head, patterns) for name, patterns in providers.items()}
                if any(volumes[name].exceeds(caps[name].hard) for name in caps):
                    return context, Verdict(False, "hard-limit"), None
                checks, validation_error = validation_receipt(self.api, self.repo, pr, policy)
                if validation_error:
                    return context, Verdict(False, validation_error), None
                if any(checks.get(name) != "success" for name in policy["promotion_checks"]):
                    return context, Verdict(False, "validation-incomplete"), None
                anchor = self.anchor(pr, statuses)
                if not anchor:
                    return context, Verdict(False, "review-anchor-not-created-in-dry-run"), None
                comments = self.api.pages(f"repos/{self.repo}/issues/{pr['number']}/comments")
                self.request_reviews(pr, comments)
                reviews = self.api.pages(f"repos/{self.repo}/pulls/{pr['number']}/reviews")
                threads = unresolved_threads(self.api, self.repo, pr["number"])
                all_paths = [os.fsdecode(p) for p in git.git("diff", "--name-only", "-z", "--no-renames", merge_base, head).stdout.split(b"\0") if p]
                receipts = {name: evidence(name, head=head, base=main, anchor_time=anchor,
                    expected_paths=[p for p in all_paths if not excluded(p, patterns)],
                    reviews=reviews, comments=comments, statuses=statuses, unresolved=threads[name])
                    for name, patterns in providers.items()}
                verdict = admit_promotion(head=head, base=main, source_repository=self.repo,
                    repository=self.repo, source_branch="develop", volumes=volumes, limits=caps,
                    reviews=receipts, checks=checks, required_checks=tuple(policy["promotion_checks"]))
            return context, verdict, (main, develop)

    def run(self):
        raw, _ = contents(self.api, self.repo, ".github/flow-policy.json", "main")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_bytes(raw)
            policy = load_policy(path)
        self.bot_login = policy.get("gate_bot_login", "kodflow-ci[bot]")
        if self.apply and not self.writer_locked(policy):
            return [{"allowed": False, "reason": "exclusive-writer-rules-missing", "merged": False}]
        open_prs = self.api.pages(f"repos/{self.repo}/pulls", parameters={"state": "open"})
        results = []
        # After a merge, stop this invocation. The next queued event refetches
        # everything. A second admission never uses the first one's old base.
        for item in sorted(open_prs, key=lambda p: (p["base"]["ref"] != "main", p["number"])):
            if item["base"]["ref"] not in {"main", "develop"}:
                continue
            pr = self.api.request("GET", f"repos/{self.repo}/pulls/{item['number']}")
            if pr["base"]["ref"] == "main" and (pr["head"]["ref"] != "develop" or (pr["head"].get("repo") or {}).get("full_name") != self.repo):
                if self.apply:
                    self.api.request("PATCH", f"repos/{self.repo}/pulls/{pr['number']}", {"base": "develop"})
                results.append({"pr": pr["number"], "allowed": False,
                                "reason": "retargeted-to-develop" if self.apply else "would-retarget-to-develop", "merged": False})
                continue
            context, verdict, refs = self.evaluate(pr, open_prs, policy)
            self.status(pr["head"]["sha"], context, "success" if verdict.allowed else "pending", verdict.reason)
            result = {"pr": pr["number"], **asdict(verdict), "merged": False}
            if (self.apply and verdict.allowed and refs and authorized_label(self.api, self.repo, pr, "flow:merge")):
                if not self.writer_locked(policy):
                    self.status(pr["head"]["sha"], context, "pending", "exclusive-writer-rules-missing")
                    result["allowed"] = False
                    result["reason"] = "exclusive-writer-rules-missing"
                elif not self.fresh(pr, *refs):
                    self.status(pr["head"]["sha"], context, "pending", "base-or-head-changed")
                    result["allowed"] = False
                    result["reason"] = "base-or-head-changed"
                else:
                    response = self.api.request("PUT", f"repos/{self.repo}/pulls/{pr['number']}/merge", {
                        "sha": pr["head"]["sha"], "merge_method": "merge"})
                    result["merged"] = response.get("merged") is True
                    results.append(result)
                    break
            results.append(result)
        return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(Controller(GitHub(), args.repository, apply=args.apply,
                                    token=os.environ.get("GH_TOKEN")).run(), indent=2))
        return 0
    except (ValueError, KeyError, TypeError, APIError, GitError, OSError) as error:
        print(json.dumps({"error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
