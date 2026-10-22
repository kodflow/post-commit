"""Migrate existing CI job graphs without guessing language-specific commands.

PyYAML is used only by deployment tooling. Runtime admission needs stdlib only.
"""

import copy
import json
import re

import yaml
from flow_candidate import candidate_job


class WorkflowLoader(yaml.SafeLoader):
    pass


# GitHub uses YAML 1.2 booleans; SafeLoader's YAML 1.1 on/off resolver loses
# the event key. Copy the resolver table so importing us never mutates PyYAML.
WorkflowLoader.yaml_implicit_resolvers = {
    char: [(tag, pattern) for tag, pattern in entries if tag != "tag:yaml.org,2002:bool"]
    for char, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
WorkflowLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(true|false)$", re.I), list("tTfF"))


def parse_workflow(text):
    value = yaml.load(text, Loader=WorkflowLoader)
    if not isinstance(value, dict) or not isinstance(value.get("jobs"), dict):
        raise ValueError("expected a GitHub workflow with a jobs mapping")
    return value


def dump_workflow(value):
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True, width=120)


def dependencies(job):
    needs = job.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    if not isinstance(needs, list) or any(not isinstance(item, str) for item in needs):
        raise ValueError("dynamic job dependencies cannot be migrated automatically")
    return needs


def combine_workflows(primary_text, included):
    """Bring standalone heavy workflows into the same promotion dependency graph."""
    primary = parse_workflow(primary_text)
    added = []
    for index, text in enumerate(included, 1):
        secondary = parse_workflow(text)
        mapping = {name: f"flow-extra-{index}-{name}" for name in secondary["jobs"]}
        def rewrite(value):
            if isinstance(value, str):
                for old, new in mapping.items():
                    value = re.sub(r"\bneeds\." + re.escape(old) + r"\b", "needs." + new, value)
                return value
            if isinstance(value, dict):
                return {key: rewrite(item) for key, item in value.items()}
            if isinstance(value, list):
                return [rewrite(item) for item in value]
            return value
        for name, job in secondary["jobs"].items():
            job = rewrite(copy.deepcopy(job))
            if "needs" in job:
                job["needs"] = [mapping[dependency] for dependency in dependencies(secondary["jobs"][name])]
            if "uses" not in job:
                job["env"] = {**secondary.get("env", {}), **job.get("env", {})}
                if "defaults" in secondary:
                    job.setdefault("defaults", copy.deepcopy(secondary["defaults"]))
                if "permissions" in secondary:
                    job.setdefault("permissions", copy.deepcopy(secondary["permissions"]))
            if mapping[name] in primary["jobs"]:
                raise ValueError("included workflow job name collision")
            primary["jobs"][mapping[name]] = job
            added.append(mapping[name])
    return dump_workflow(primary), added


def migrate_graph_validator(text, source_path, backup):
    original = 'ci = read("' + source_path + '")'
    if text.count(original) != 1:
        raise ValueError("unsupported project graph validator")
    return text.replace(original, 'ci = read("' + backup + '")')


def main_events(workflow, *, preserve_push=False):
    """Keep manual/reusable/tag events; restrict automatic qualification to main PRs."""
    events = workflow.get("on", {})
    if isinstance(events, str):
        events = {events: None}
    elif isinstance(events, list):
        events = {name: None for name in events}
    if not isinstance(events, dict):
        raise ValueError("unrecognized workflow events")
    events = copy.deepcopy(events)
    if "pull_request_target" in events:
        raise ValueError("privileged pull_request_target workflows require a dedicated migration")
    if "pull_request" in events:
        old = events["pull_request"] or {}
        types = old.get("types", ["opened", "synchronize", "reopened", "ready_for_review"])
        events["pull_request"] = {"branches": ["main"], "types": types}
    # Keep tag publication; don't run the qualification a second time on a
    # main push. A separate release workflow continues to build its own output.
    if "push" in events and not preserve_push:
        push = events.pop("push")
        if isinstance(push, dict) and ("tags" in push or "tags-ignore" in push):
            events["push"] = {key: value for key, value in push.items() if key in {"tags", "tags-ignore"}}
    workflow["on"] = events


def migrate_workflow(text, spec):
    """spec selects existing jobs and matrix values, preserving their actual code."""
    full = parse_workflow(text)
    original = copy.deepcopy(full)
    jobs = original["jobs"]
    lanes = spec["lanes"]
    if set(lanes) != {"lint", "unit", "heavy"} or any(not values for values in lanes.values()):
        raise ValueError("lint, unit and heavy must each identify existing required jobs")
    if any(name not in jobs for values in lanes.values() for name in values):
        raise ValueError("qualification profile names an unknown job")
    if any("ci-" + lane in jobs for lane in lanes):
        raise ValueError("qualification summary job name collides with an existing job")
    main_events(full, preserve_push=spec.get("preserve_push", False))
    if "flow-candidate" in jobs:
        raise ValueError("candidate job already exists")
    # Full qualification must test the merge candidate, including when the
    # original workflow used a head SHA for builds or external test dispatch.
    def candidate_refs(value):
        if isinstance(value, str):
            return value.replace("github.event.pull_request.head.sha || github.sha", "github.sha").replace("github.event.pull_request.head.sha", "github.sha")
        if isinstance(value, dict):
            return {key: candidate_refs(item) for key, item in value.items()}
        if isinstance(value, list):
            return [candidate_refs(item) for item in value]
        return value
    full = candidate_refs(full)
    if spec.get("external_e2e"):
        dispatch = full["jobs"][spec["external_e2e"]["job_id"]]
        for step in dispatch.get("steps", []):
            if "REF" in step.get("env", {}):
                step["env"]["REF"] = "${{ github.sha }}"
    for job_name, job in full["jobs"].items():
        if job_name != "post-commit":
            job["needs"] = list(dict.fromkeys(["flow-candidate", *dependencies(job)]))
        for step in job.get("steps", []):
            if step.get("uses", "").startswith("actions/checkout@") and not step.get("with", {}).get("repository"):
                step.setdefault("with", {})["ref"] = "${{ github.sha }}"
    full["jobs"]["flow-candidate"] = candidate_job()
    for lane, needed in lanes.items():
        full["jobs"]["ci-" + lane] = {
            "name": "ci-" + lane, "runs-on": "ubuntu-latest", "timeout-minutes": 5,
            "if": "${{ always() && github.event_name == 'pull_request' }}",
            "needs": list(dict.fromkeys(needed)), "permissions": {},
            "steps": [{"name": "Require every selected job to succeed", "env": {
                "RESULTS": "${{ toJSON(needs) }}",
            }, "run": "python3 - <<'PY'\nimport json, os\nr = json.loads(os.environ['RESULTS'])\nif not r or any(v['result'] != 'success' for v in r.values()):\n    raise SystemExit('Required validation missing, skipped, cancelled or failed')\nPY\n"}],
        }
    # Lint/unit are mandatory on the integrated promotion even when legacy
    # path detection would have skipped them. Existing dependency failures
    # still prevent execution through the normal needs semantics.
    for name in set(lanes["lint"] + lanes["unit"]):
        full["jobs"][name].pop("if", None)
    promotion = "github.event_name != 'pull_request' || (github.head_ref == 'develop' && github.event.pull_request.head.repo.full_name == github.repository)"
    for job_name, job in full["jobs"].items():
        if job_name == "post-commit":
            continue  # Keep the independent governance producer during bootstrap.
        old = job.get("if", "success()")
        if isinstance(old, bool):
            old = str(old).lower()
        old = str(old).strip()
        if old.startswith("${{") and old.endswith("}}"):
            old = old[3:-2].strip()
        if "${{" in old:
            raise ValueError("unsupported compound job condition")
        job["if"] = "${{ (" + promotion + ") && (" + old + ") }}"

    selected = set(spec["advisory_jobs"])
    if not selected or not selected <= jobs.keys():
        raise ValueError("advisory_jobs must select existing lightweight jobs")
    pending = list(selected)
    while pending:
        name = pending.pop()
        for dependency in dependencies(jobs[name]):
            if dependency not in jobs:
                raise ValueError("workflow has an unknown dependency")
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    # Do not silently drag a heavy prerequisite onto develop.
    if not selected <= set(spec.get("advisory_allowed_dependencies", spec["advisory_jobs"])):
        raise ValueError("lightweight CI depends on jobs not approved for develop")
    advisory = {key: copy.deepcopy(value) for key, value in original.items() if key not in {"on", "jobs", "name", "concurrency"}}
    advisory.update({
        "name": str(original.get("name", "CI")) + " (develop advisory)",
        "on": {"pull_request": {"branches": ["develop"], "types": ["opened", "synchronize", "reopened", "ready_for_review", "edited"]}},
        "concurrency": {"group": "flow-advisory-${{ github.event.pull_request.number }}", "cancel-in-progress": True},
        "jobs": {key: copy.deepcopy(job) for key, job in jobs.items() if key in selected},
    })
    for name, matrix in spec.get("advisory_matrix", {}).items():
        if name not in selected:
            raise ValueError("matrix override names an unselected advisory job")
        existing = advisory["jobs"][name].get("strategy", {}).get("matrix")
        if not isinstance(existing, dict) or not isinstance(matrix, dict):
            raise ValueError("matrix override requires a static existing matrix")
        for dimension, values in matrix.items():
            if not isinstance(values, list) or not values or not set(values) <= set(existing.get(dimension, [])):
                raise ValueError("advisory matrix must be a nonempty subset of the original")
            existing[dimension] = values
    return dump_workflow(full), dump_workflow(advisory)


def configure_reviewers(coderabbit_text, qodo_text):
    """Preserve authored instructions while limiting automatic reviews to main."""
    import tomllib
    coderabbit = yaml.load(coderabbit_text or "{}", Loader=WorkflowLoader) or {}
    auto = coderabbit.setdefault("reviews", {}).setdefault("auto_review", {})
    # Request reviews centrally once full CI is green, never on every push.
    auto.update({"enabled": False, "drafts": False, "base_branches": [], "labels": [],
                 "auto_incremental_review": False})
    qodo = tomllib.loads(qodo_text or "")
    ignored = [pattern for pattern in qodo.get("config", {}).get("ignore_pr_target_branches", [])
               if not re.search(pattern, "main")]
    if "^(?!main$).*" not in ignored:
        ignored.append("^(?!main$).*")
    sources = [pattern for pattern in qodo.get("config", {}).get("ignore_pr_source_branches", [])
               if not re.search(pattern, "develop")]
    if "^(?!develop$).*" not in sources:
        sources.append("^(?!develop$).*")
    # Preserve prompts and unrelated settings, refusing ambiguous multiline edits.
    lines = (qodo_text or "").splitlines()
    def assign(section, key, value):
        nonlocal lines
        start = next((i for i, line in enumerate(lines) if re.match(r"^\s*\[" + re.escape(section) + r"\]\s*(?:#.*)?$", line)), None)
        assignment = key + " = " + json.dumps(value)
        if start is None:
            lines += ["", "[" + section + "]", assignment]
        else:
            end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*\[[A-Za-z0-9_.-]+\]\s*(?:#.*)?$", lines[i])), len(lines))
            matches = [i for i in range(start + 1, end) if re.match(r"\s*" + re.escape(key) + r"\s*=", lines[i])]
            if matches:
                index = matches[0]
                for stop in range(index + 1, end + 1):
                    try:
                        tomllib.loads("\n".join(lines[index:stop]))
                    except tomllib.TOMLDecodeError:
                        continue
                    lines[index:stop] = [assignment]
                    break
                else:
                    raise ValueError("cannot locate the end of the Qodo assignment safely")
            else:
                lines.insert(start + 1, assignment)
    assign("config", "ignore_pr_target_branches", ignored)
    assign("config", "ignore_pr_source_branches", sources)
    assign("github_app", "pr_commands", [])
    assign("github_app", "push_commands", [])
    assign("github_app", "handle_push_trigger", False)
    assign("review_agent", "enable_incremental_review", False)
    output = "\n".join(lines).strip() + "\n"
    tomllib.loads(output)
    return dump_workflow(coderabbit), output
