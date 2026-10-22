"""Run admission centrally with the existing per-owner installation tokens."""

import argparse
import json
import os
from pathlib import Path

from flow_api import APIError, GitHub
from flow_controller import Controller
from flow_git import GitError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default=str(Path(__file__).with_name("projects.json")))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("targets", nargs="*")
    args = parser.parse_args(argv)
    registry = json.loads(Path(args.registry).read_text())
    targets = args.targets or sorted(registry["projects"])
    original = os.environ.get("GH_TOKEN")
    errors = []
    for repo in targets:
        if repo not in registry["projects"]:
            errors.append({"repository": repo, "error": "project profile not registered"})
            continue
        owner = repo.split("/")[0]
        key = "FLEET_TOKEN_" + owner.upper().replace("-", "_")
        token = os.environ.get(key) or original
        if not token:
            errors.append({"repository": repo, "error": "installation token unavailable"})
            continue
        os.environ["GH_TOKEN"] = token
        try:
            api = GitHub()
            api.request("GET", f"repos/{repo}")  # A 404 here is missing access, not an absent migration.
            results = Controller(api, repo, apply=args.apply, token=token).run()
            print(json.dumps({"repository": repo, "results": results}))
        except APIError as error:
            if error.status == 404 and error.endpoint.split("?")[0].endswith("/.github/flow-policy.json"):
                print(json.dumps({"repository": repo, "state": "migration-not-installed"}))
            else:
                errors.append({"repository": repo, "error": str(error)})
        except (ValueError, KeyError, TypeError, OSError, GitError) as error:
            errors.append({"repository": repo, "error": str(error)})
        finally:
            if original is None:
                os.environ.pop("GH_TOKEN", None)
            else:
                os.environ["GH_TOKEN"] = original
    for error in errors:
        print(json.dumps(error))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
