"""Small GitHub REST transport; secrets stay in gh's environment, never argv."""

import json
import re
import subprocess
from urllib.parse import urlencode


class APIError(RuntimeError):
    def __init__(self, endpoint, status=None):
        self.endpoint = endpoint
        self.status = status
        super().__init__(f"GitHub request failed: {endpoint.split('?')[0]} (HTTP {status or 'unknown'})")


class GitHub:
    def request(self, method, endpoint, payload=None, *, raw=False):
        if not re.fullmatch(r"(?:repos|orgs|apps|app)/[^\s]+|graphql", endpoint):
            raise ValueError("only GitHub API endpoints are accepted")
        args = ["gh", "api", "--method", method, "-H", "Accept: application/vnd.github+json", endpoint]
        data = None
        if payload is not None:
            args += ["--input", "-"]
            data = json.dumps(payload).encode()
        result = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode:
            match = re.search(rb"HTTP (\d{3})", result.stderr)
            raise APIError(endpoint, int(match[1]) if match else None)
        if raw:
            return result.stdout
        return json.loads(result.stdout) if result.stdout else None

    def pages(self, endpoint, field=None, parameters=None):
        values = []
        for page in range(1, 1001):
            query = urlencode({**(parameters or {}), "per_page": 100, "page": page})
            response = self.request("GET", endpoint + "?" + query)
            items = response[field] if field else response
            if not isinstance(items, list):
                raise APIError(endpoint)
            values.extend(items)
            if len(items) < 100:
                return values
        raise APIError(endpoint)


def repository_name(value):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("repository must be owner/name")
    return value


def authorized_label(api, repository, pr, name, *, admin_only=False):
    """Only an explicit label application by a writer authorizes an exception."""
    if name not in {item["name"] for item in pr.get("labels", [])}:
        return False
    events = api.pages(f"repos/{repository}/issues/{pr['number']}/events")
    for event in reversed(events):
        if event.get("label", {}).get("name") != name:
            continue
        if event.get("event") != "labeled":
            return False
        login = event.get("actor", {}).get("login", "")
        if not re.fullmatch(r"[A-Za-z0-9_\[\]-]+", login):
            return False
        permission = api.request("GET", f"repos/{repository}/collaborators/{login}/permission")
        return permission.get("permission") in ({"admin"} if admin_only else {"admin", "maintain", "write"})
    return False


def reconcile_rules(api, endpoint, desired, *, apply=False):
    """Update only controller-owned names, never inherited or unrelated rules."""
    existing = api.pages(endpoint)
    changes = []
    for wanted in desired:
        matches = [r for r in existing if r["name"] == wanted["name"]]
        if len(matches) > 1:
            raise ValueError(f"duplicate managed ruleset: {wanted['name']}")
        if matches:
            item = matches[0]
            if item.get("source_type") not in (None, "Repository" if endpoint.startswith("repos/") else "Organization"):
                raise ValueError("refusing to overwrite an inherited ruleset")
            current = api.request("GET", f"{endpoint}/{item['id']}")
            # Compare every field we own, including enforcement and bypasses.
            if all(current.get(key) == value for key, value in wanted.items()):
                continue
            destination = f"{endpoint}/{item['id']}"
            method = "PUT"
        else:
            destination, method = endpoint, "POST"
        changes.append({"method": method, "endpoint": destination, "payload": wanted})
        if apply:
            api.request(method, destination, wanted)
    return changes
