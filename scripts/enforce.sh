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
#               · NO bypass actors. An admin bypass would make the whole thing
#                 advisory: `gh pr merge --admin` merges straight through a red
#                 gate, and the one account that would use it is the one the
#                 gate exists to constrain. A rewrite still has a way through —
#                 disable the ruleset, push, restore it — but that is a
#                 deliberate, logged act, not a flag on a merge command.
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
# usage: enforce.sh [--apply] [--audit] [--report FILE] (--all | owner/repo ...)
#        enforce.sh --selftest
#   dry-run unless --apply. --audit only reads: it answers the three questions
#   that decide whether the gate is real on a repository — is the workflow
#   there, can the required status actually be enforced, and can anyone walk
#   past it. Needs `gh` authenticated as an admin of the targets.
#   --selftest touches no network: it pins the drift comparison in both
#   directions (see RUNNER_OVERRIDES) and is what CI runs.
# ============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STUB_FILE="$SCRIPT_DIR/../stub/post-commit.yml"
STUB_PATH=".github/workflows/post-commit.yml"
BRANCH="chore/post-commit"
LEGACY_BRANCHES=("chore/commit-guard")
RULESET_NAME="post-commit"
# This repository gates itself through ci.yml, so it has no stub by design.
SELF_REPO="kodflow/post-commit"
GITHUB_ACTIONS_APP_ID=15368
# The runner the stub asks for. Nothing substitutes ON this value — an
# override replaces whatever label the gate job carries — but the selftest
# checks against it that `block-merge` was left alone, so it is written down
# once rather than spelled out at each use.
STUB_RUNNER="ubuntu-latest"

# Repositories whose gate job legitimately runs somewhere other than the stub's
# runner, and the label it runs on instead. `owner/repo=label`, one per line.
#
# supervizio/agent and supervizio/libprobe are PRIVATE, so every run of this
# gate — every pull-request commit, every push to the trunk, every manual check
# — bills GitHub-hosted minutes, rounded UP to a whole minute per job, for a
# composite action that is actions/checkout plus bash. At the observed merge
# rate that is on the order of 560 billable minutes a month across the two, and
# it is what exhausted libprobe's allowance four times. The same run on the
# self-hosted ARC pool costs nothing and lands some 15-25s slower — measured at
# 26s on agent and 36s on libprobe against 9-18s hosted.
#
# supervizio/runner-template is deliberately NOT here and must not be added: it
# is PUBLIC, its hosted minutes are free, and pointing a public repository's
# pull requests at a self-hosted runner would let an untrusted fork run code on
# the fleet. That asymmetry is the whole reason this is per-repository and not
# an edit to the stub — a stub change would be actively wrong for that repo.
#
# An override relaxes exactly one line of exactly one job. Everything else in
# the deployed file is still required to match the stub, so a repository listed
# here is NOT exempt from the next stub change; see stub_covered_by.
#
# And it only ever applies to a repository that is PRIVATE when the run looks.
# Visibility is not a property of this list — anyone with admin can flip it — so
# it is read on every run, and a listed repository found public (or internal, or
# unreadable) never gets the label: it is held to the stub, which runs hosted.
# Being listed here is necessary, never sufficient.
RUNNER_OVERRIDES=(
    "supervizio/agent=supervizio-runner"
    "supervizio/libprobe=supervizio-runner"
)
APPLY=false; AUDIT=false; SELFTEST=false; REPORT=""; TARGETS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY=true ;;
        --audit) AUDIT=true ;;
        --selftest) SELFTEST=true ;;
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
$SELFTEST || [ "${#TARGETS[@]}" -gt 0 ] || { echo "usage: enforce.sh [--apply] [--audit] [--report FILE] (--all | owner/repo ...)" >&2; exit 2; }
[ -r "$STUB_FILE" ] || { echo "stub not found: $STUB_FILE" >&2; exit 2; }
STUB_B64="$(base64 -w0 < "$STUB_FILE")"
STUB_BLOB="$(git hash-object "$STUB_FILE")"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
# Real newlines, not `\n`. `gh pr create --body` takes this string literally, so
# the escapes that used to be here reached the pull request as the two
# characters backslash and n — every sync body on the fleet rendered as one
# run-on paragraph with `\n` printed in the middle of it.
SYNC_BODY='The gate workflow in this repository has drifted from the central stub in
[kodflow/post-commit](https://github.com/kodflow/post-commit/blob/main/stub/post-commit.yml).

The rules themselves live in the action and are pinned to `@main`, so they were
already current here. What was not is everything the stub itself carries — the
inputs it passes, and the jobs that react to a verdict.

This replaces the file with the central copy verbatim. Nothing in it is
repository-specific.'
# Appended for a repository carrying a runner override: the file this opens is
# rendered, not copied, so the label survives — but any commentary added
# locally does not, because the sync writes the central copy.
OVERRIDE_NOTE='

### This repository carries a runner override

The `runs-on:` of the gate job is not the stub value. That is deliberate and
central — it lives in `RUNNER_OVERRIDES` in
[scripts/enforce.sh](https://github.com/kodflow/post-commit/blob/main/scripts/enforce.sh),
which is also where the reason is written down — and this pull request keeps it.
What it does not keep is any comment added to the file inside this repository:
the sync writes the central copy, annotated only by the override.'
# Appended when a listed repository is not private. The sync then writes the
# stub unmodified, which moves the gate job back to a hosted runner, and the
# reader deserves to know that is the point rather than a side effect.
REFUSED_NOTE='

### This moves the gate back to a hosted runner, on purpose

This repository is listed in `RUNNER_OVERRIDES` in
[scripts/enforce.sh](https://github.com/kodflow/post-commit/blob/main/scripts/enforce.sh)
but it is not private. On a repository anyone can fork, a gate job on a
self-hosted runner runs the pull requests of strangers on that runner — so the
override is refused here, and this pull request writes the stub as it is.
Merge it, then remove the entry from `RUNNER_OVERRIDES`.'

ruleset_payload() {
    jq -n --arg name "$RULESET_NAME" --argjson app "$GITHUB_ACTIONS_APP_ID" '{
        name: $name, target: "branch", enforcement: "active",
        bypass_actors: [],
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

runner_override() {   # runner_override <owner/repo> -> the label, or nothing
    local repo="$1" entry
    # `set -u` and an empty array are not friends on every bash this runs on,
    # and an empty list is a realistic end state — the day both repositories
    # come back to the stub, this file should need one deletion, not two.
    [ "${#RUNNER_OVERRIDES[@]}" -gt 0 ] || return 1
    for entry in "${RUNNER_OVERRIDES[@]}"; do
        [ "${entry%%=*}" = "$repo" ] && { printf '%s' "${entry#*=}"; return 0; }
    done
    return 1
}

# The stub carries two `runs-on:` lines and they are not interchangeable. The
# gate job is the one that runs on every pull-request commit, so it is the one
# whose minutes matter; `block-merge` holds a `pull-requests: write` token,
# runs only after a failure, and stays hosted on purpose. Matching the job by
# name rather than by position is what keeps an override from ever landing on
# the wrong one — and if the stub renames the job, nothing is substituted and
# this reports an error instead of silently rendering the stub unchanged.
render_stub() {   # render_stub <stub> <label> <job> <outfile>
    awk -v label="$2" -v job="$3" '
        # Re-evaluated at EVERY job key, not just ours. Latched on, a gate job
        # that lost its runs-on line would hand the override to the next job
        # down — which is block-merge, the one holding the write token.
        $0 ~ /^  [A-Za-z0-9_.-]+:[[:space:]]*$/ { injob = ($0 ~ "^  " job ":[[:space:]]*$") }
        injob && !hit && $0 ~ /^    runs-on:/ { print "    runs-on: " label; hit = 1; next }
        { print }
        END { exit(hit ? 0 : 1) }
    ' "$1" > "$4"
}

# Is the deployed file an acceptable rendering of the stub?
#
# For a repository with no override this is never asked: the deployed blob sha
# IS a git blob sha, so one API call settles it exactly. An overridden
# repository cannot be checked that way — the label differs by construction,
# and both repositories that carry an override also carry a comment block
# explaining the choice at the point the reader meets it.
#
# Stripping every comment before comparing would accept that, and would gut the
# guard while doing it: this stub is mostly comments, and they are the reasoning
# the repository exists to carry. A stub whose comments can rot on two thirds of
# the fleet is the drift this script was written to catch.
#
# So the contract is narrower than equality and far stronger than similarity:
# every line of the rendered stub must appear in the deployed file, in order,
# byte for byte, and every line the deployed file adds on top must be a comment
# or blank. A repository may annotate. It may not edit, reorder or drop a single
# line, and it may not add anything that executes. Change one comment in the
# stub and the line it replaced is no longer there to be found — drift, on the
# overridden repositories exactly as on every other one.
#
# "A comment" is a YAML fact, not a textual one, and inside a block scalar
# (`run: |`, `if: >-`) it does not hold: a `#` line there is content — shell in
# a script, text in an expression — and a blank line is a newline. An
# annotation dropped into block-merge's folded `if:` puts `#` inside `${{ }}`,
# which GitHub refuses to parse, so the whole workflow stops loading and the
# gate never reports; one dropped after a line continuation in a `run:` script
# cuts a command in two. So where an extra line may go is read off the stub's
# own structure. Between the first and last content line of a block scalar,
# nothing. After its last content line, until the line that ends it: blank
# lines only if the scalar does not keep trailing ones (`|+`), and the first
# comment must sit no deeper than the key that opened the scalar, or YAML reads
# it as more content. Everywhere else, any comment or blank line. Extra lines
# are space-indented only — a tab where YAML expects indentation is an error.
#
# This is checked against a real YAML parser in the selftest, at every gap of
# the stub, and the check is the stricter of the two by design: it may refuse
# an annotation YAML would have tolerated, never accept one that changes a value.
stub_covered_by() {   # stub_covered_by <rendered-stub> <deployed-file>
    awk '
        function lead(s) { match(s, /^ */); return RLENGTH }
        # Classify the gaps a closed scalar leaves: 1 = inside it (sealed),
        # 2 = its tail, carrying the opening key depth, keep, and an id.
        function close_scalar(term,   g) {
            for (g = hdr; g < lastc; g++) cls[g] = 1
            for (g = lastc; g < term; g++) { cls[g] = 2; th[g] = hind; tk[g] = hkeep; ts[g] = hdr }
            insc = 0
        }
        FNR == 1 { pass++ }
        pass == 1 {
            want[++n] = $0
            if (insc) {
                if ($0 ~ /^ *$/) next                       # interior or trailing: what follows decides
                if (lead($0) > hind) { lastc = n; next }     # content
                close_scalar(n)                              # this line ends it, and may open another
            }
            if ($0 ~ /^ *[^# ][^#]*:[ ]+[|>][-+0-9]*[ ]*(#.*)?$/ || $0 ~ /^ *-[ ]+[|>][-+0-9]*[ ]*(#.*)?$/) {
                insc = 1; hdr = n; lastc = n; hind = lead($0)
                tok = $0; sub(/^.*[:-][ ]+/, "", tok); sub(/[ ]*(#.*)?$/, "", tok)
                hkeep = (index(tok, "+") > 0)
            }
            next
        }
        !ready { if (insc) close_scalar(n + 1); ready = 1 }   # a scalar that runs to the end
        bad { next }
        {
            if (i < n && $0 == want[i + 1]) { i++; next }
            # An extra line, in the gap after want[i].
            if (cls[i] == 1) { bad = 1; next }                 # inside a scalar: content, whatever it looks like
            if ($0 !~ /^ *(#.*)?$/) { bad = 1; next }          # only comments and blank lines
            if (cls[i] == 2 && closed != ts[i]) {              # the scalar may still be open
                if ($0 ~ /^ *$/) { if (tk[i] || length($0) > th[i]) bad = 1; next }
                if (lead($0) > th[i]) { bad = 1; next }        # deep enough to be read as content
                closed = ts[i]                                 # this comment ended the scalar
            }
        }
        END { exit((!bad && i == n) ? 0 : 1) }
    ' "$1" "$2"
}

ensure_stub() {   # -> sets STUB_STATE, PR_URL, STUB_NOTE
    local repo="$1" db="$2" sha out
    PR_URL=""; STUB_NOTE=""
    # This repository gates itself through ci.yml (`uses: ./`, job named
    # post-commit) so the version under review is the one that runs; a stub
    # pinned to @main would test the wrong code. The ruleset still applies.
    if [ "$repo" = "$SELF_REPO" ]; then STUB_STATE="self"; return; fi
    # The deployed blob's sha IS a git blob sha, so comparing it with
    # `git hash-object` on our copy is an exact content check for one API call
    # — no download, no normalising base64 line wrapping. Without this the stub
    # was only ever checked for existence, so every later change to it (the
    # identity input, the pull-request comment) sat undeployed on a fleet that
    # reported itself complete.
    # A repository listed in RUNNER_OVERRIDES is compared against the stub as
    # rendered for it, and that rendered copy is also what a sync would write —
    # so --apply can never push the stub's runner back onto a private repo and
    # quietly restart the meter it was moved off.
    local label="" want_blob="$STUB_BLOB" want_b64="$STUB_B64" want_file="$STUB_FILE" vis
    if label="$(runner_override "$repo")"; then
        # Listed is not enough: the override is for a PRIVATE repository, and
        # visibility is live state, so it is read now rather than assumed from
        # the list. Anything else fails closed. Public or internal: the label is
        # refused and the repository is held to the stub, which runs hosted, so
        # a sync repairs it instead of blessing it. Unreadable: nothing at all —
        # neither the self-hosted label nor a sync built on a guess.
        vis="$(gh api "repos/$repo" --jq .visibility 2>/dev/null)"
        case "$vis" in
            private) ;;
            public|internal)
                STUB_NOTE="override refused: $vis"
                echo "::error::$repo is $vis but listed in RUNNER_OVERRIDES. A self-hosted gate on a $vis repository runs other people's pull requests on the fleet, so the override is refused and the repository is held to the stub (hosted). Remove the entry." >&2
                label="" ;;
            *) STUB_STATE="error:visibility"; return ;;
        esac
    fi
    if [ -n "$label" ]; then
        want_file="$WORKDIR/rendered.yml"
        render_stub "$STUB_FILE" "$label" post-commit "$want_file" \
            || { STUB_STATE="error:render"; return; }
        want_blob="$(git hash-object "$want_file")"
        want_b64="$(base64 -w0 < "$want_file")"
    fi

    local deployed verb title
    deployed="$(gh api "repos/$repo/contents/$STUB_PATH?ref=$db" --jq .sha 2>/dev/null)"
    if [ -n "$deployed" ] && [ "$deployed" = "$want_blob" ]; then
        STUB_STATE="present"; return
    fi
    # Only an overridden repository is allowed to be annotated, and only that
    # case pays for the download the blob-sha comparison above exists to avoid.
    # A download that fails is not evidence of drift: reading it as drift would
    # open a sync, on a guess, that strips the repository's annotations.
    if [ -n "$deployed" ] && [ -n "$label" ]; then
        if ! gh api "repos/$repo/contents/$STUB_PATH?ref=$db" --jq .content 2>/dev/null \
                | base64 -d > "$WORKDIR/deployed.yml" 2>/dev/null \
           || [ ! -s "$WORKDIR/deployed.yml" ]; then
            STUB_STATE="error:download"; return
        fi
        if stub_covered_by "$want_file" "$WORKDIR/deployed.yml"; then
            STUB_STATE="present:override"; return
        fi
    fi
    if [ -n "$deployed" ]; then verb=sync; else verb=add; fi

    for b in "$BRANCH" "${LEGACY_BRANCHES[@]}"; do
        PR_URL="$(gh pr list --repo "$repo" --head "$b" --state open --json url --jq '.[0].url // empty' 2>/dev/null)"
        [ -n "$PR_URL" ] && { STUB_STATE="pr-open"; return; }
    done
    if ! $APPLY; then
        [ "$verb" = sync ] && STUB_STATE="stale:would-sync" || STUB_STATE="would-open-pr"
        return
    fi

    sha="$(gh api "repos/$repo/git/ref/heads/$db" --jq .object.sha 2>/dev/null)" || { STUB_STATE="error:no-sha"; return; }
    gh api "repos/$repo/git/refs" -X POST -f ref="refs/heads/$BRANCH" -f sha="$sha" >/dev/null 2>&1 \
        || gh api "repos/$repo/git/refs/heads/$BRANCH" >/dev/null 2>&1 \
        || { STUB_STATE="error:branch"; return; }

    # Updating an existing file needs the blob sha it is replacing, and the one
    # on the branch is not always the one on the default branch.
    local onbranch args=()
    onbranch="$(gh api "repos/$repo/contents/$STUB_PATH?ref=$BRANCH" --jq .sha 2>/dev/null)"
    [ -n "$onbranch" ] && args=(-f "sha=$onbranch")

    if [ "$verb" = sync ]; then
        title="ci(post-commit): sync the gate workflow with the central stub"
    else
        title="ci: add the mandatory post-commit gate"
    fi
    out="$(gh api "repos/$repo/contents/$STUB_PATH" -X PUT -f message="$title" \
            -f content="$want_b64" -f branch="$BRANCH" "${args[@]}" 2>&1)" \
        || { STUB_STATE="error:put:${out:0:60}"; return; }

    if [ "$verb" = sync ]; then
        PR_URL="$(gh pr create --repo "$repo" --base "$db" --head "$BRANCH" --title "$title" \
            --body "$SYNC_BODY${label:+$OVERRIDE_NOTE}${STUB_NOTE:+$REFUSED_NOTE}" 2>&1 | grep -oE 'https://[^ ]+' | head -1)"
        [ -n "$PR_URL" ] && STUB_STATE="sync-created" || STUB_STATE="error:pr"
    else
        PR_URL="$(gh pr create --repo "$repo" --base "$db" --head "$BRANCH" --title "$title" \
            --body-file "$SCRIPT_DIR/../stub/pr-body.md" 2>&1 | grep -oE 'https://[^ ]+' | head -1)"
        [ -n "$PR_URL" ] && STUB_STATE="pr-created" || STUB_STATE="error:pr"
    fi
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


# --- selftest ---------------------------------------------------------------
# A runner override exists so that two repositories stop being reported as
# drift. The failure mode of that kind of change is a comparison that can no
# longer fail at all — which is strictly worse than no comparison, because it
# reports green forever and nobody looks again. These cases pin both
# directions: what an override must accept, and what it must still refuse.
# Network-free, so tests/run.sh runs them on every pull request here. What they
# cannot reach — the orchestration in ensure_stub, visibility, --apply — is
# driven end to end against a fake gh in tests/run.sh.
if $SELFTEST; then
    t_pass=0; t_fail=0
    t() {   # t <name> <want-rc> <got-rc>
        if [ "$2" -eq "$3" ]; then t_pass=$((t_pass + 1)); printf '  ok   %s\n' "$1"
        else t_fail=$((t_fail + 1)); printf '  FAIL %s (want rc %s, got %s)\n' "$1" "$2" "$3"; fi
    }
    R="$WORKDIR/rendered.yml"; D="$WORKDIR/deployed.yml"
    covered() {   # covered <awk-program>: build D from R with it, then compare
        awk "$1" "$R" > "$D" && stub_covered_by "$R" "$D"
    }

    echo "== overrides =="
    # The public repository of this fleet, named so that listing it is a red
    # test and not a quiet edit. The live guard is the visibility check in
    # ensure_stub, which refuses any repository that is not private; this is the
    # tripwire in front of it.
    runner_override supervizio/runner-template >/dev/null
    t "supervizio/runner-template has no override" 1 $?
    for entry in "${RUNNER_OVERRIDES[@]}"; do
        # Non-empty, not just found: `owner/repo=` would render `runs-on:` with
        # nothing after it, and that file does not load at all.
        if [ -n "$(runner_override "${entry%%=*}")" ]; then rc=0; else rc=1; fi
        t "${entry%%=*} resolves to a non-empty label" 0 "$rc"
    done

    echo "== render =="
    render_stub "$STUB_FILE" self-hosted-x post-commit "$R"
    t "the gate job's runs-on is substituted" 0 $?
    grep -qx "    runs-on: self-hosted-x" "$R"; t "...to the override label" 0 $?
    grep -qx "    runs-on: $STUB_RUNNER" "$R"; t "block-merge keeps the stub runner" 0 $?
    [ "$(grep -c '^    runs-on:' "$R")" -eq "$(grep -c '^    runs-on:' "$STUB_FILE")" ]
    t "no runs-on line is added or lost" 0 $?
    render_stub "$STUB_FILE" self-hosted-x no-such-job "$WORKDIR/x.yml"
    t "a job the stub does not have is an error, not a silent no-op" 1 $?
    # The override must never be able to walk downhill into the next job. Only
    # the GATE job loses its runner here: block-merge keeps its own, because a
    # renderer latched on after `post-commit:` needs a runs-on further down to
    # wrongly take. With both removed, as this fixture once did, the latched
    # renderer finds nothing, fails for the wrong reason, and the test passes
    # with the very regression it names.
    awk '
        /^  [A-Za-z0-9_.-]+:[[:space:]]*$/ { gate = ($0 == "  post-commit:") }
        gate && /^    runs-on:/ { next }
        { print }
    ' "$STUB_FILE" > "$WORKDIR/noruns.yml"
    grep -qx "    runs-on: $STUB_RUNNER" "$WORKDIR/noruns.yml"
    t "the fixture leaves block-merge a runner to wrongly take" 0 $?
    render_stub "$WORKDIR/noruns.yml" self-hosted-x post-commit "$WORKDIR/x.yml"
    t "a gate job with no runs-on does not hand the label to the next job" 1 $?
    ! grep -q 'runs-on: self-hosted-x' "$WORKDIR/x.yml"
    t "...and block-merge is not relabelled" 0 $?

    echo "== accepted =="
    cp "$R" "$D"; stub_covered_by "$R" "$D"
    t "byte-identical to the rendering" 0 $?
    covered '/^    runs-on:/ { print "    # why this repository differs"; print "    #"; print "" } { print }'
    t "comments and blanks added on top" 0 $?
    # Exactly where agent and libprobe annotate: after block-merge's folded if:,
    # at the job's own depth, which ends the scalar before the comment starts.
    covered '/^    runs-on: ubuntu-latest$/ { print "    # stays hosted, on purpose" } { print }'
    t "a comment after the if: scalar, at the depth of its key" 0 $?
    covered '/^    runs-on: ubuntu-latest$/ { print "" } { print }'
    t "a blank line after a scalar that does not keep them" 0 $?
    covered '/^      - env:$/ { print "      # about the next step" } { print }'
    t "a comment between two steps, after a run: script" 0 $?

    echo "== refused =="
    # The point of the whole design: an overridden repository is still held to
    # every other line of the stub, comments included.
    sed 's|^# post-commit — mandatory merge gate.$|# post-commit - reworded locally|' "$R" > "$D"
    stub_covered_by "$R" "$D"; t "a stub comment reworded locally" 1 $?
    grep -v '^  block-merge:$' "$R" > "$D"
    stub_covered_by "$R" "$D"; t "a stub line dropped" 1 $?
    covered '/^    timeout-minutes: 10$/ { print "    continue-on-error: true" } { print }'
    t "an executable line added" 1 $?
    tac "$R" > "$D" 2>/dev/null || tail -r "$R" > "$D"
    stub_covered_by "$R" "$D"; t "the same lines in another order" 1 $?
    # An override relaxes one line of one job, not the label everywhere: the
    # token-bearing block-merge job must not be able to follow it off-hosted.
    render_stub "$STUB_FILE" self-hosted-x block-merge "$D"
    stub_covered_by "$R" "$D"; t "block-merge moved to the override label" 1 $?
    # And it is not a blanket pass for the repository either: the file that has
    # not taken the override is as much drift as any other mismatch.
    stub_covered_by "$R" "$STUB_FILE"; t "the unrendered stub against an override" 1 $?
    # Inside a block scalar a `#` line is content. Each of these looks like an
    # annotation and is not one; the first two stop the workflow from loading.
    covered '{ print } /^      \$\{\{ failure\(\)$/ { print "      # inside the expression" }'
    t "a comment inside block-merge's folded if:" 1 $?
    covered '{ print } /^      \$\{\{ failure\(\)$/ { print "" }'
    t "a blank line inside block-merge's folded if:" 1 $?
    covered '{ print } /^          MARKER=/ { print "          # inside the script" }'
    t "a comment inside a run: script" 1 $?
    covered '{ print } /--paginate \\$/ { print "                # after a line continuation" }'
    t "a comment after a line continuation in a run: script" 1 $?
    covered '/^    runs-on: ubuntu-latest$/ { print "          # deeper than the if: key" } { print }'
    t "a comment after a scalar, deep enough to continue it" 1 $?
    covered '/^  post-commit:$/ { print; print "\t# tab-indented"; next } { print }'
    t "a tab-indented comment" 1 $?

    echo "== oracle: every gap of the stub, against a YAML parser =="
    # The rules above are a model of YAML; this holds the model to YAML itself.
    # A probe goes into every gap of the rendered stub — a blank line, a
    # whitespace-only line, a comment at each depth, and the sequences that open
    # and then close a scalar's tail — and whatever the check accepts must parse
    # to exactly what the stub parses to. Refusing what YAML would tolerate is
    # allowed. Accepting what changes a value is the bug.
    py="${PC_YAML_PYTHON:-python3}"
    if "$py" -c 'import yaml' 2>/dev/null; then
        o="$WORKDIR/oracle"; mkdir -p "$o"
        made="$(awk -v dir="$o" '
            function pad(d,   s) { s = ""; while (d-- > 0) s = s " "; return s }
            { line[NR] = $0 }
            END {
                np = split("B S12 C0 C2 C4 C6 C8 C10 C12 B,C12 B,C4 C4,C12 C4,B,C12", probe, " ")
                for (g = 0; g <= NR; g++) for (p = 1; p <= np; p++) {
                    f = sprintf("%s/%04d-%02d.yml", dir, g, p)
                    for (k = 1; k <= g; k++) print line[k] > f
                    m = split(probe[p], part, ",")
                    for (q = 1; q <= m; q++) {
                        kind = substr(part[q], 1, 1); d = substr(part[q], 2) + 0
                        if (kind == "B") print "" > f
                        else if (kind == "S") print pad(d) > f
                        else print pad(d) "# probe" > f
                    }
                    for (k = g + 1; k <= NR; k++) print line[k] > f
                    close(f); made++
                }
                print made
            }' "$R")"
        for f in "$o"/*.yml; do
            if stub_covered_by "$R" "$f"; then echo "${f##*/} accept"; else echo "${f##*/} refuse"; fi
        done | LC_ALL=C sort > "$o/check.txt"
        "$py" - "$R" "$o" <<'PY' | LC_ALL=C sort > "$o/yaml.txt"
import os, sys, yaml
L = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
def load(path):
    with open(path) as fh:
        return yaml.load(fh, Loader=L)
ref = load(sys.argv[1])
for f in os.listdir(sys.argv[2]):
    if f.endswith(".yml"):
        try:
            same = load(os.path.join(sys.argv[2], f)) == ref
        except yaml.YAMLError:
            same = False
        print(f, "same" if same else "differs")
PY
        read -r total acc unsound biting lenient < <(LC_ALL=C join "$o/check.txt" "$o/yaml.txt" | awk '
            { n++ } $2 == "accept" { a++ } $2 == "accept" && $3 == "differs" { u++ }
            $2 == "refuse" && $3 == "differs" { b++ } $2 == "refuse" && $3 == "same" { l++ }
            END { print n + 0, a + 0, u + 0, b + 0, l + 0 }')
        [ "$total" -eq "$made" ] && [ "$made" -gt 0 ]
        t "oracle: every one of the $made probes got both verdicts" 0 $?
        [ "$unsound" -eq 0 ]
        t "oracle: nothing accepted changes what YAML reads ($acc accepted)" 0 $?
        LC_ALL=C join "$o/check.txt" "$o/yaml.txt" | awk '$2 == "accept" && $3 == "differs" { print "       unsound: " $1 }' | head -5
        [ "$acc" -gt 0 ] && [ "$biting" -gt 0 ]
        t "oracle: not vacuous — $biting real changes refused" 0 $?
        echo "  (refused although YAML would not have minded: $lenient — the conservative side, by design)"
    else
        echo "  skip oracle: no python with PyYAML here (CI has one; PC_YAML_PYTHON points at another)"
    fi

    echo ""
    echo "passed: $t_pass  failed: $t_fail"
    [ "$t_fail" -eq 0 ]; exit $?
fi

# --- audit ------------------------------------------------------------------
# Read-only. A ruleset that exists is not the same as a gate that holds: it can
# carry bypass actors, in which case `gh pr merge --admin` walks straight
# through a red status and the whole thing is advice. And GitHub refuses
# rulesets outright on a private repository in a Free-plan org, so there the
# status can run but can never be required. Both cases print here.
if $AUDIT; then
    printf '%-40s %-8s %-10s %-12s %s\n' REPOSITORY VISIBLE WORKFLOW REQUIRED BYPASS
    ok=0; advisory=0; total=0
    for repo in "${TARGETS[@]}"; do
        db="$(gh repo view "$repo" --json defaultBranchRef --jq '.defaultBranchRef.name // empty' 2>/dev/null)"
        # An empty repository is listed but not counted: there is no history to
        # gate and no default branch to attach a ruleset to, so scoring it as a
        # gap invents work that does not exist.
        [ -n "$db" ] || { printf '%-40s %-8s %-10s %-12s %s\n' "$repo" - empty - -; continue; }
        total=$((total + 1))
        vis="$(gh api "repos/$repo" --jq 'if .private then "private" else "public" end' 2>/dev/null)"
        if gh api "repos/$repo/contents/$STUB_PATH?ref=$db" --jq .content >/dev/null 2>&1 \
           || [ "$repo" = "$SELF_REPO" ]; then wf=yes; else wf=NO; fi
        raw="$(gh api "repos/$repo/rulesets" 2>&1)"
        if printf '%s' "$raw" | grep -q 'Upgrade to GitHub'; then
            req="unavailable"; byp="-"
        else
            id="$(printf '%s' "$raw" | jq -r --arg n "$RULESET_NAME" '.[]|select(.name==$n)|.id' 2>/dev/null | head -1)"
            if [ -z "$id" ]; then req=NO; byp="-"
            else
                req=yes
                byp="$(gh api "repos/$repo/rulesets/$id" --jq '[.bypass_actors[]?]|length' 2>/dev/null)"
                [ "$byp" = "0" ] || byp="$byp BYPASSABLE"
            fi
        fi
        # Three outcomes, not two. A repository GitHub refuses a ruleset on is
        # not the same failure as one where nobody created it: the first is a
        # plan limit with nothing left to do here, the second is a gap someone
        # can close in a command. Counting them together turns a permanent,
        # understood limit into noise that hides the real gaps.
        if [ "$wf" = yes ] && [ "$req" = yes ] && [ "$byp" = "0" ]; then
            ok=$((ok + 1))
        elif [ "$wf" = yes ] && [ "$req" = unavailable ]; then
            advisory=$((advisory + 1))
        fi
        printf '%-40s %-8s %-10s %-12s %s\n' "$repo" "$vis" "$wf" "$req" "$byp"
    done
    echo ""
    gaps=$((total - ok - advisory))
    echo "enforced: $ok / $total"
    [ "$advisory" -gt 0 ] && echo "advisory: $advisory (private repo in a Free-plan org — GitHub allows no ruleset; the gate runs and comments, nothing blocks the merge)"
    [ "$gaps" -gt 0 ] && echo "gaps:     $gaps (missing workflow, missing ruleset, or a ruleset with bypass actors)"
    :
    exit 0
fi

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
        present|present:*|self|stale:*|sync-created) ensure_rule "$repo" ;;
        *)            RULE_STATE="deferred:stub-not-merged" ;;
    esac
    ROWS+=("$repo"$'\t'"$db"$'\t'"$STUB_STATE${STUB_NOTE:+ ($STUB_NOTE)}"$'\t'"$RULE_STATE"$'\t'"$PR_URL")
    printf '%-40s stub=%-16s rule=%-20s %s%s\n' "$repo" "$STUB_STATE" "$RULE_STATE" "$PR_URL" "${STUB_NOTE:+ [$STUB_NOTE]}"
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
