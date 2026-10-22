"""Offline plan/evaluation interface. No command merges or changes GitHub."""

import argparse
import json
import sys
from dataclasses import asdict

from flow_core import admit_integration
from flow_git import GitError, Repository
from flow_policy import limits, load_policy, rulesets


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser("rulesets", help="print repository or scoped organization rules")
    render.add_argument("--policy", required=True)
    render.add_argument("--organization-repositories", nargs="+")
    simulate = commands.add_parser("simulate", help="evaluate integration against real Git objects")
    simulate.add_argument("--policy", required=True)
    simulate.add_argument("--repo", required=True)
    simulate.add_argument("--main", default="main")
    simulate.add_argument("--develop", default="develop")
    simulate.add_argument("--head", required=True)
    simulate.add_argument("--promotion-open", action="store_true")
    simulate.add_argument("--authorized-correction", action="store_true",
                          help="local operator input; a server controller must verify authorization")
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        if args.command == "rulesets":
            print(json.dumps(rulesets(policy, args.organization_repositories), indent=2))
            return 0
        repository = Repository(args.repo)
        snapshot = repository.simulate(args.main, args.develop, args.head, {
            name: item["exclude"] for name, item in policy["reviewers"].items()})
        verdict = admit_integration(
            snapshot.before, snapshot.after, limits(policy),
            promotion_open=args.promotion_open,
            correction_authorized=args.authorized_correction,
        )
        # Catch a local ref moving during simulation. The future merge
        # controller must repeat this check in its serialized transaction.
        repository.assert_current(snapshot, args.main, args.develop, args.head)
        print(json.dumps({"verdict": asdict(verdict), "snapshot": asdict(snapshot)}, indent=2))
        return 0 if verdict.allowed else 1
    except (ValueError, KeyError, TypeError, GitError, OSError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
