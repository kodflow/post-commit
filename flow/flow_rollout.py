"""Plan/deploy migration PRs, then reconcile protection after installation.

The default is dry-run. No required check is installed before its producer.
The central enforce workflow already supplies owner-scoped App tokens.
"""

import argparse
import copy
import json
import os
from pathlib import Path

from flow_api import APIError, GitHub, reconcile_rules, repository_name
from flow_controller import blob_sha, contents
from flow_migrate import combine_workflows, configure_reviewers, main_events, migrate_workflow, parse_workflow, dump_workflow
from flow_policy import rulesets, validate_policy


BRANCH = "chore/develop-promotion"
POLICY_PATH = ".github/flow-policy.json"
MANAGED_RULE_NAMES = {"flow-develop", "flow-main", "flow-writer"}


def check_release_dependency(workflow, source, spec, included):
    events = workflow.get("on") or {}
    if not isinstance(events, dict):
        return
    trigger = events.get("workflow_run") or {}
    dependencies = trigger.get("workflows", [])
    if isinstance(dependencies, str):
        dependencies = [dependencies]
    # Preserve existing release contracts rather than silently removing their
    # upstream push run. Included workflows become manual-only and need a
    # dedicated migration when another pipeline consumes their completion.
    removed = {parse_workflow(text).get("name") for text in included}
    if not spec.get("preserve_push", False):
        removed.add(parse_workflow(source).get("name"))
    import fnmatch
    if any(fnmatch.fnmatchcase(name, pattern) for name in removed if name
           for pattern in dependencies):
        raise ValueError("downstream workflow_run dependency requires an explicit release migration or preserve_push profile")


def optional_file(api, repo, path, ref="main"):
    try:
        return contents(api, repo, path, ref)[0].decode()
    except APIError as error:
        if error.status == 404:
            return None
        raise


def migration_files(api, repo, default_policy, spec):
    repository_name(repo)
    source_path = spec["workflow"]
    backup = ".github/flow-source/" + source_path.rsplit("/", 1)[1]
    source = optional_file(api, repo, backup)
    if source is None:
        source = optional_file(api, repo, source_path)
    if source is None or blob_sha(source) != spec["source_sha"]:
        raise ValueError(f"{repo}: qualification workflow changed; update the reviewed project profile")
    included = []
    included_files = {}
    for path, expected in spec.get("include_workflows", {}).items():
        saved = ".github/flow-source/" + path.rsplit("/", 1)[1]
        text = optional_file(api, repo, saved)
        if text is None:
            text = optional_file(api, repo, path)
        if text is None or blob_sha(text) != expected:
            raise ValueError(f"{repo}: included heavy workflow changed: {path}")
        included.append(text)
        included_files[saved] = text
        manual = parse_workflow(text)
        manual["on"] = {"workflow_dispatch": (manual.get("on") or {}).get("workflow_dispatch")}
        included_files[path] = dump_workflow(manual)
    combined, added = combine_workflows(source, included)
    combined_spec = copy.deepcopy(spec)
    combined_spec["lanes"]["heavy"] += added
    full, advisory = migrate_workflow(combined, combined_spec)
    cr, qodo = configure_reviewers(optional_file(api, repo, ".coderabbit.yaml"),
                                   optional_file(api, repo, ".pr_agent.toml"))
    files = {**included_files, backup: source, source_path: full, ".github/workflows/flow-advisory.yml": advisory,
             ".coderabbit.yaml": cr, ".pr_agent.toml": qodo}
    # Existing secondary PR validations also move to promotion, preserving
    # their authored jobs. They need not run on every integration PR.
    listing = api.request("GET", f"repos/{repo}/contents/.github/workflows?ref=main")
    for item in listing:
        path = item["path"]
        if path in files or path.endswith(("/post-commit.yml", "/enforce.yml", "/flow-control.yml")):
            continue
        if not path.endswith((".yml", ".yaml")):
            continue
        text = optional_file(api, repo, path)
        workflow = parse_workflow(text)
        check_release_dependency(workflow, source, spec, included)
        events = workflow.get("on", {})
        names = {events} if isinstance(events, str) else set(events or {})
        if "pull_request_target" in names:
            raise ValueError(f"{repo}: privileged secondary workflow requires explicit review: {path}")
        if "pull_request" in names:
            main_events(workflow)
            files[path] = dump_workflow(workflow)
    policy = copy.deepcopy(default_policy)
    import yaml, tomllib
    from flow_migrate import WorkflowLoader
    cr_config = yaml.load(cr, Loader=WorkflowLoader)
    filters = cr_config.get("reviews", {}).get("path_filters", [])
    if any(not value.startswith("!") for value in filters):
        raise ValueError("positive CodeRabbit path selectors require an explicit budget profile")
    policy["reviewers"]["coderabbit"]["exclude"] = sorted(set(
        policy["reviewers"]["coderabbit"]["exclude"] + [value[1:] for value in filters]))
    qodo_ignore = tomllib.loads(qodo).get("ignore", {})
    if qodo_ignore.get("regex"):
        raise ValueError("Qodo regex exclusions require an explicit budget profile")
    policy["reviewers"]["qodo"]["exclude"] = sorted(set(
        policy["reviewers"]["qodo"]["exclude"] + qodo_ignore.get("glob", [])))
    policy.update({"qualification_workflow": source_path,
                   "managed_files": {path: blob_sha(text) for path, text in files.items()}})
    files[POLICY_PATH] = json.dumps(policy, indent=2, sort_keys=True) + "\n"
    validate_policy(policy)
    # Never republish a literal credential hidden in a legacy workflow.
    import re
    credential = re.compile(r"(?:AKIA[A-Z0-9]{16}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")
    for path, text in files.items():
        if credential.search(text):
            raise ValueError(f"{repo}: possible credential in {path}; migration refused")
    return files, policy


def install_pr(api, repo, files, base, *, apply=False):
    current = {path: optional_file(api, repo, path, base) for path in files}
    changed = {path: text for path, text in files.items() if current[path] != text}
    if not changed:
        return {"state": "installed"}
    if not apply:
        return {"state": "would-open-migration", "base": base, "paths": sorted(changed)}
    prs = api.pages(f"repos/{repo}/pulls", parameters={"state": "open", "head": repo.split("/")[0] + ":" + BRANCH})
    # Updating an existing migration could replace a human's edits. Report it
    # instead; the operator closes/recreates or intentionally updates the PR.
    if prs:
        return {"state": "migration-open", "url": prs[0]["html_url"]}
    main = api.request("GET", f"repos/{repo}/git/ref/heads/{base}")["object"]["sha"]
    try:
        previous = api.request("GET", f"repos/{repo}/git/ref/heads/{BRANCH}")
    except APIError as error:
        if error.status != 404:
            raise
        previous = None
    if previous is not None:
        raise ValueError(f"{repo}: existing migration branch without an open PR; preserve and inspect it")
    commit = api.request("GET", f"repos/{repo}/git/commits/{main}")
    tree = api.request("POST", f"repos/{repo}/git/trees", {"base_tree": commit["tree"]["sha"], "tree": [
        {"path": path, "mode": "100644", "type": "blob", "content": text} for path, text in changed.items()]})
    new = api.request("POST", f"repos/{repo}/git/commits", {
        "message": "ci(flow): separate permissive integration from qualified promotion",
        "tree": tree["sha"], "parents": [main]})
    api.request("POST", f"repos/{repo}/git/refs", {"ref": "refs/heads/" + BRANCH, "sha": new["sha"]})
    pr = api.request("POST", f"repos/{repo}/pulls", {
        "title": "ci(flow): integrate on develop and qualify promotions to main", "head": BRANCH, "base": base,
        "body": "Integration lint/unit checks remain visible but nonblocking on develop. Main promotions run the complete existing CI, including lint/unit, and require Qodo/CodeRabbit evidence. Review soft/hard limits reserve correction capacity. This PR installs producers first; branch protections are reconciled only after installation. Existing CI commands are retained under .github/flow-source/. For policy updates after enrollment, a maintainer must authorize flow:policy on the update PR and on its promotion.",
    })
    return {"state": "migration-created", "url": pr["html_url"]}


def protect(api, repo, policy, *, apply=False):
    metadata = api.request("GET", f"repos/{repo}")
    if apply:
        existing_labels = {label["name"] for label in api.pages(f"repos/{repo}/labels")}
        for name, description in {
            "flow:merge": "Request serialized admission and merge",
            "flow:correction": "Authorize use of the correction reserve during promotion",
            "flow:policy": "Administrator-approved update of managed CI policy files",
        }.items():
            if name not in existing_labels:
                api.request("POST", f"repos/{repo}/labels", {"name": name, "color": "0969da", "description": description})
    desired = rulesets(policy)
    if metadata["owner"]["type"] == "Organization":
        owner, name = repo.split("/")
        endpoint = f"orgs/{owner}/rulesets"
        try:
            existing = api.pages(endpoint)
            included = {name}
            for item in existing:
                if item["name"] in MANAGED_RULE_NAMES:
                    detail = api.request("GET", endpoint + f"/{item['id']}")
                    included.update(detail.get("conditions", {}).get("repository_name", {}).get("include", []))
            plan = reconcile_rules(api, endpoint, rulesets(policy, sorted(included)), apply=apply)
            return {"scope": "organization", "changes": plan}
        except APIError as error:
            if error.status not in {403, 404}:
                raise
            # An unavailable plan or permission is reported, not mistaken for
            # successful organization enforcement. Repo rules remain real.
            fallback = f"organization-rules-unavailable-http-{error.status}"
    else:
        fallback = "personal-account"
    return {"scope": "repository", "reason": fallback,
            "changes": reconcile_rules(api, f"repos/{repo}/rulesets", desired, apply=apply)}


def rollout(api, repo, default_policy, spec, *, apply=False, output_dir=None):
    metadata = api.request("GET", f"repos/{repo}")
    if metadata.get("archived") or metadata.get("fork"):
        return {"state": "skipped-archived-or-fork"}
    if metadata.get("default_branch") != "main":
        raise ValueError(f"{repo}: main must remain the default branch for CodeRabbit promotion-only reviews")
    files, policy = migration_files(api, repo, default_policy, spec)
    if output_dir:
        destination = Path(output_dir) / repo
        for path, text in files.items():
            if Path(path).is_absolute() or ".." in Path(path).parts:
                raise ValueError("invalid migration output path")
            output = destination / path
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_name(output.name + ".tmp")
            temporary.write_text(text)
            temporary.replace(output)
    # Install to main before protection, then use develop for later updates.
    installed = all(optional_file(api, repo, path) == text for path, text in files.items())
    if not installed:
        existing = optional_file(api, repo, POLICY_PATH)
        base = "develop" if existing else "main"
        return install_pr(api, repo, files, base, apply=apply)
    try:
        api.request("GET", f"repos/{repo}/git/ref/heads/develop")
    except APIError as error:
        if error.status != 404:
            raise
        if not apply:
            return {"state": "would-create-develop-before-protection"}
        main = api.request("GET", f"repos/{repo}/git/ref/heads/main")["object"]["sha"]
        api.request("POST", f"repos/{repo}/git/refs", {"ref": "refs/heads/develop", "sha": main})
    # A preexisting develop must also have producers, not just main.
    if any(optional_file(api, repo, path, "develop") != text for path, text in files.items()):
        return install_pr(api, repo, files, "develop", apply=apply)
    result = protect(api, repo, policy, apply=apply)
    return {"state": "protected" if apply else "would-reconcile-protection", **result}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default=str(Path(__file__).with_name("projects.json")))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--output-dir", help="write reviewable migration files locally, including in dry-run mode")
    parser.add_argument("targets", nargs="*")
    args = parser.parse_args(argv)
    registry = json.loads(Path(args.registry).read_text())
    validate_policy(registry["policy"])
    targets = args.targets or sorted(registry["projects"])
    original = os.environ.get("GH_TOKEN")
    failed = False
    for repo in targets:
        try:
            repository_name(repo)
            if repo not in registry["projects"]:
                raise ValueError("project needs an explicit CI classification before migration")
            key = "FLEET_TOKEN_" + repo.split("/")[0].upper().replace("-", "_")
            if os.environ.get(key):
                os.environ["GH_TOKEN"] = os.environ[key]
            result = rollout(GitHub(), repo, registry["policy"], registry["projects"][repo], apply=args.apply and not args.audit, output_dir=args.output_dir)
            if args.audit and (result["state"] != "would-reconcile-protection" or result.get("changes")):
                failed = True
            print(json.dumps({"repository": repo, **result}))
        except (APIError, ValueError, KeyError, TypeError, OSError) as error:
            failed = True
            print(json.dumps({"repository": repo, "error": str(error)}))
        finally:
            if original is None:
                os.environ.pop("GH_TOKEN", None)
            else:
                os.environ["GH_TOKEN"] = original
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
