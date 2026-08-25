#!/usr/bin/env bash
# ============================================================================
# enforce.sh — make the `post-commit` status mandatory on every repository.
#
# For each target (forks, archived and empty repos are skipped):
#   1. stub — ensure .github/workflows/post-commit.yml is on the default
#             branch; if not, ensure a PR adding it is open.
#   2. rule — ONLY once the stub is on the default branch, ensure a repository
#             ruleset named "post-commit" targets that branch with:
#               · required status check `post-commit`, accepted only from
#                 GitHub Actions (integration 15368) so nobody can post a
#                 look-alike status with a PAT;
#               · no branch deletion, no non-fast-forward push (the
#                 force-push protection the local hook used to provide);
#               · bypass: repository admins, always — "only me, at the limit".
# A required status that is never reported blocks the merge, so once the
# ruleset is in place deleting or renaming the stub blocks every PR: the
# gate cannot be removed from below.
#
# Both steps are idempotent; re-running never duplicates a PR or a ruleset.
# Re-run after the stub PR merges to put the ruleset on: until then the repo
# is reported as `deferred:stub-not-merged`.
# Repos on a plan without rulesets (private repos in a Free org) are
# reported as UNAVAILABLE — GitHub cannot enforce anything there.
#
# usage: enforce.sh [--apply] [--report FILE] (--all | owner/repo ...)
#   dry-run unless --apply. Needs `gh` authenticated as an admin of the targets.
# ============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STUB_FILE="$SCRIPT_DIR/../stub/post-commit.yml"
STUB_PATH=".github/workflows/post-commit.yml"
BRANCH="chore/post-commit"
LEGACY_BRANCHES=("chore/commit-guard")
RULESET_NAME="post-commit"
GITHUB_ACTIONS_APP_ID=15368
APPLY=false; REPORT=""; TARGETS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY=true ;;
        --report) REPORT="$2"; shift ;;
        --all)
            OWNER="$(gh api user --jq .login)"
            mapfile -t ORGS < <(gh api user/orgs --jq '.[].login')
            for o in "$OWNER" "${ORGS[@]}"; do
                while IFS= read -r r; do TARGETS+=("$r"); done < <(
                    gh repo list "$o" --limit 1000 --json nameWithOwner,isArchived,isFork,defaultBranchRef \
                        --jq '.[] | select(.isArchived==false and .isFork==false and .defaultBranchRef!=null) | .nameWithOwner')
            done ;;
        -*) echo "unknown option: $1" >&2; exit 2 ;;
        *) TARGETS+=("$1") ;;
    esac
    shift
done
[ "${#TARGETS[@]}" -gt 0 ] || { echo "usage: enforce.sh [--apply] [--report FILE] (--all | owner/repo ...)" >&2; exit 2; }
[ -r "$STUB_FILE" ] || { echo "stub not found: $STUB_FILE" >&2; exit 2; }
STUB_B64="$(base64 -w0 < "$STUB_FILE")"

ruleset_payload() {
    jq -n --arg name "$RULESET_NAME" --argjson app "$GITHUB_ACTIONS_APP_ID" '{
        name: $name, target: "branch", enforcement: "active",
        bypass_actors: [{actor_id: 5, actor_type: "RepositoryRole", bypass_mode: "always"}],
        conditions: {ref_name: {include: ["~DEFAULT_BRANCH"], exclude: []}},
        rules: [
            {type: "deletion"},
            {type: "non_fast_forward"},
            {type: "required_status_checks", parameters: {
                strict_required_status_checks_policy: false,
                do_not_enforce_on_create: false,
                required_status_checks: [{context: "post-commit", integration_id: $app}]}}
        ]}'
}

ensure_stub() {   # -> sets STUB_STATE, PR_URL
    local repo="$1" db="$2" sha out
    PR_URL=""
    # This repository gates itself through ci.yml (`uses: ./`, job named
    # post-commit) so the version under review is the one that runs; a stub
    # pinned to @main would test the wrong code. The ruleset still applies.
    if [ "$repo" = "kodflow/post-commit" ]; then STUB_STATE="self"; return; fi
    if gh api "repos/$repo/contents/$STUB_PATH?ref=$db" --jq .sha >/dev/null 2>&1; then
        STUB_STATE="present"; return
    fi
    for b in "$BRANCH" "${LEGACY_BRANCHES[@]}"; do
        PR_URL="$(gh pr list --repo "$repo" --head "$b" --state open --json url --jq '.[0].url // empty' 2>/dev/null)"
        [ -n "$PR_URL" ] && { STUB_STATE="pr-open"; return; }
    done
    $APPLY || { STUB_STATE="would-open-pr"; return; }
    sha="$(gh api "repos/$repo/git/ref/heads/$db" --jq .object.sha 2>/dev/null)" || { STUB_STATE="error:no-sha"; return; }
    gh api "repos/$repo/git/refs" -X POST -f ref="refs/heads/$BRANCH" -f sha="$sha" >/dev/null 2>&1 \
        || gh api "repos/$repo/git/refs/heads/$BRANCH" >/dev/null 2>&1 \
        || { STUB_STATE="error:branch"; return; }
    out="$(gh api "repos/$repo/contents/$STUB_PATH" -X PUT -f message="ci: add the mandatory post-commit gate" \
            -f content="$STUB_B64" -f branch="$BRANCH" 2>&1)" || { STUB_STATE="error:put:${out:0:60}"; return; }
    PR_URL="$(gh pr create --repo "$repo" --base "$db" --head "$BRANCH" \
        --title "ci: add the mandatory post-commit gate" \
        --body-file "$SCRIPT_DIR/../stub/pr-body.md" 2>&1 | grep -oE 'https://[^ ]+' | head -1)"
    [ -n "$PR_URL" ] && STUB_STATE="pr-created" || STUB_STATE="error:pr"
}

ensure_rule() {   # -> sets RULE_STATE
    local repo="$1" existing id out
    existing="$(gh api "repos/$repo/rulesets" 2>&1)" || {
        case "$existing" in
            *"Upgrade to GitHub"*) RULE_STATE="unavailable:plan" ;;
            *) RULE_STATE="error:${existing:0:60}" ;;
        esac; return; }
    id="$(printf '%s' "$existing" | jq -r --arg n "$RULESET_NAME" '.[] | select(.name==$n) | .id' | head -1)"
    if [ -n "$id" ]; then
        $APPLY || { RULE_STATE="present"; return; }
        out="$(ruleset_payload | gh api "repos/$repo/rulesets/$id" -X PUT --input - 2>&1)" \
            && RULE_STATE="synced" || RULE_STATE="error:put:${out:0:60}"
        return
    fi
    $APPLY || { RULE_STATE="would-create"; return; }
    out="$(ruleset_payload | gh api "repos/$repo/rulesets" -X POST --input - 2>&1)" \
        && RULE_STATE="created" || RULE_STATE="error:post:${out:0:80}"
}

ROWS=()
for repo in "${TARGETS[@]}"; do
    db="$(gh repo view "$repo" --json defaultBranchRef --jq '.defaultBranchRef.name // empty' 2>/dev/null)"
    if [ -z "$db" ]; then ROWS+=("$repo"$'\t'"-"$'\t'"skip:empty"$'\t'"-"$'\t'""); echo "$repo: empty, skipped"; continue; fi
    ensure_stub "$repo" "$db"
    # The ruleset goes on only once the stub is actually on the default
    # branch. Creating it first makes `post-commit` a required status that no
    # workflow reports yet, which blocks EVERY open pull request in the
    # repository — dependabot bumps included — until the stub PR merges.
    # devcontainer-template spent a day in exactly that state.
    case "$STUB_STATE" in
        present|self) ensure_rule "$repo" ;;
        *)            RULE_STATE="deferred:stub-not-merged" ;;
    esac
    ROWS+=("$repo"$'\t'"$db"$'\t'"$STUB_STATE"$'\t'"$RULE_STATE"$'\t'"$PR_URL")
    printf '%-40s stub=%-16s rule=%-20s %s\n' "$repo" "$STUB_STATE" "$RULE_STATE" "$PR_URL"
done

if [ -n "$REPORT" ]; then
    {
        echo "## post-commit enforcement ($($APPLY && echo applied || echo dry-run))"
        echo ""
        echo "| Repository | Branch | Stub | Ruleset | PR |"
        echo "|---|---|---|---|---|"
        for row in "${ROWS[@]}"; do
            IFS=$'\t' read -r r b s u p <<< "$row"
            printf '| %s | %s | %s | %s | %s |\n' "$r" "$b" "$s" "$u" "${p:+[link]($p)}"
        done
    } >> "$REPORT"
fi
