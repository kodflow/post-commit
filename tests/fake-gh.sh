#!/usr/bin/env bash
# A stand-in for `gh`, so tests can drive scripts/enforce.sh end to end with no
# network: the real script, the real control flow, only the API is fake.
#
# It answers the calls enforce.sh makes from files under
# $FAKE_GH_DIR/<owner>/<repo>/ and appends every invocation to
# $FAKE_GH_DIR/calls.log. A call it does not recognise fails with exit 97 and
# names itself. That is deliberate: a double that answers anything would let a
# new, unmocked API call through as a quiet success, and a test built on it
# would pass for the wrong reason.
#
#   visibility      private | public | internal      absent: the lookup fails
#   workflow.yml    the deployed .github/workflows/post-commit.yml; absent: 404
#   content_fails   present: downloading that file's content fails
#   open_pr         a URL: an open pull request exists on the enforce branch
#
# For the test to inspect afterwards: put.b64 (the content of a contents PUT)
# and pr.body (the body of a pull request it was asked to create).
set -uo pipefail
D="${FAKE_GH_DIR:?FAKE_GH_DIR is not set}"
printf '%s\n' "$*" >> "$D/calls.log"

die() { printf 'fake-gh: %s\n' "$*" >&2; exit "${RC:-1}"; }
unknown() { RC=97 die "unmocked call: gh $ARGS"; }
ARGS="$*"

jqf=""; method=GET; endpoint=""; content=""; repo=""; body=""
sub="${1:-}"; shift || true
while [ $# -gt 0 ]; do
    case "$1" in
        --jq) jqf="$2"; shift ;;
        -X) method="$2"; shift ;;
        -f|-F) case "$2" in content=*) content="${2#content=}" ;; esac; shift ;;
        --input) [ "$2" = - ] && cat > /dev/null; shift ;;
        --repo|-R) repo="$2"; shift ;;
        --body) body="$2"; shift ;;
        --body-file) body="$(cat "$2")"; shift ;;
        --json|--head|--base|--state|--title) shift ;;
        --paginate) ;;
        -*) unknown ;;
        *) [ -z "$endpoint" ] && endpoint="$1" ;;
    esac
    shift
done

out() {   # out <json>: print it, through --jq when one was given (raw, like gh)
    if [ -n "$jqf" ]; then jq -r "$jqf" <<< "$1"; else printf '%s\n' "$1"; fi
}
file_json() {   # file_json <path>: the contents API's answer for one file
    local b64
    b64="$(base64 < "$1" | tr -d '\n' | fold -w 60)"
    jq -n --arg s "$(git hash-object "$1")" --arg c "$b64" '{sha:$s, content:$c, encoding:"base64"}'
}

case "$sub" in
    repo)   # gh repo view <owner/repo> --json defaultBranchRef --jq ...
        [ "$endpoint" = view ] || unknown
        out '{"defaultBranchRef":{"name":"main"}}' ;;
    pr)
        case "$endpoint" in
            list)
                if [ -s "$D/$repo/open_pr" ]; then out "$(jq -n --arg u "$(cat "$D/$repo/open_pr")" '[{url:$u}]')"
                else out '[]'; fi ;;
            create)
                printf '%s' "$body" > "$D/$repo/pr.body"
                echo "https://github.com/$repo/pull/4242" ;;
            *) unknown ;;
        esac ;;
    api)
        path="${endpoint%%\?*}"; query=""
        case "$endpoint" in *\?*) query="${endpoint#*\?}" ;; esac
        rest="${path#repos/}"; owner="${rest%%/*}"; rest="${rest#*/}"
        name="${rest%%/*}"; tail="${rest#"$name"}"; R="$D/$owner/$name"
        case "$method $tail" in
            "GET ")
                [ -s "$R/visibility" ] || die "HTTP 502 (visibility lookup failed)"
                out "$(jq -n --arg v "$(cat "$R/visibility")" '{visibility:$v, private:($v=="private")}')" ;;
            "GET /contents/.github/workflows/post-commit.yml")
                case "$query" in
                    ref=main)
                        [ -s "$R/workflow.yml" ] || die "Not Found (HTTP 404)"
                        [ "$jqf" = .content ] && [ -e "$R/content_fails" ] && die "HTTP 500 (content download failed)"
                        out "$(file_json "$R/workflow.yml")" ;;
                    ref=chore/post-commit) die "Not Found (HTTP 404)" ;;
                    *) unknown ;;
                esac ;;
            "PUT /contents/.github/workflows/post-commit.yml")
                printf '%s' "$content" > "$R/put.b64"; out '{"content":{}}' ;;
            "GET /git/ref/heads/main") out '{"object":{"sha":"0000000000000000000000000000000000000001"}}' ;;
            "POST /git/refs") out '{"ref":"refs/heads/chore/post-commit"}' ;;
            "GET /git/refs/heads/chore/post-commit") out '{"ref":"refs/heads/chore/post-commit"}' ;;
            "GET /rulesets") out '[{"id":1,"name":"post-commit"}]' ;;
            "PUT /rulesets/1"|"POST /rulesets") out '{"id":1}' ;;
            *) unknown ;;
        esac ;;
    *) unknown ;;
esac
