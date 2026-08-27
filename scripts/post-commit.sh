#!/usr/bin/env bash
# ============================================================================
# post-commit.sh — the merge gate behind the `post-commit` status.
#
# Server-side twin of devcontainer-template's git hooks (.githooks/commit-msg,
# the content half of .claude/scripts/git-guard.sh). Those run on the
# developer's machine and are skipped by `git commit --no-verify`; this runs
# on GitHub against commits that already exist, so there is nothing to skip.
#
# Three checks, each switchable:
#   1. identity    — every commit's author AND committer is one of the allowed
#                    accounts (PC_AUTHORS). A repository's history must carry
#                    the owning GitHub account, not a personal identity.
#   2. trailers    — every identity trailer (Co-authored-by, Signed-off-by, …)
#                    names an allowed account too. The author/committer fields
#                    are not the only place an identity lands: a trailer body
#                    carries one just as durably, and a personal address there
#                    is the same leak. Structural on purpose — it never names
#                    a person, so this public repo stays free of the addresses
#                    it exists to keep out of histories.
#   3. attribution — no AI attribution in ANY commit reachable from the head
#                    (PC_HISTORY=full, the default) or in the range only
#                    (PC_HISTORY=range). Also inspects author AND committer
#                    identity: a commit authored as "Claude <noreply@…>"
#                    carries no trailer at all.
#   4. format      — conventional-commit subject on the range's non-merge
#                    commits (project convention, see devcontainer-template).
#   5. secrets     — no credential-shaped ADDED lines in the range's diff.
#
# Deliberately NOT here: lint/build/test. Every repo's own CI already runs
# those server-side, so --no-verify never bypassed them in the first place.
#
# usage: post-commit.sh <head-rev> [<range>]
#   head-rev  commit whose whole ancestry is scanned when PC_HISTORY=full
#   range     A..B — scope of format/secrets, and of attribution when
#             PC_HISTORY=range. Omit for a history-only scan (local audit).
#
# exit 0 = clean · 1 = violations · 2 = usage/internal error
# ============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HEAD_REV="${1:-}"
RANGE="${2:-}"
STRICT="${PC_STRICT:-false}"
SECRETS="${PC_SECRETS:-true}"
HISTORY="${PC_HISTORY:-full}"
FORMAT="${PC_FORMAT:-true}"
AUTHORS="${PC_AUTHORS:-}"
MAX_REPORT="${PC_MAX_REPORT:-50}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"

if [ -z "$HEAD_REV" ]; then
    echo "usage: post-commit.sh <head-rev> [<range>]" >&2
    exit 2
fi
if [ "$HISTORY" = "range" ] && [ -z "$RANGE" ]; then
    echo "::error::PC_HISTORY=range needs a range argument" >&2
    exit 2
fi

# --- Patterns ---------------------------------------------------------------
# Comments and blank lines stripped. Case-insensitivity is applied once, by
# the runner, instead of being baked into each pattern.
PATTERNS=()
load_patterns() {
    local f="$1" line
    [ -r "$f" ] || { echo "::error::pattern file not readable: $f" >&2; exit 2; }
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue ;; esac
        PATTERNS+=("$line")
    done < "$f"
}
load_patterns "$SCRIPT_DIR/patterns.txt"
[ "$STRICT" = "true" ] && load_patterns "$SCRIPT_DIR/patterns-strict.txt"
if [ "${#PATTERNS[@]}" -eq 0 ]; then
    echo "::error::no patterns loaded — refusing to report a false clean" >&2
    exit 2
fi

# --- Allowed identities -----------------------------------------------------
# PC_AUTHORS is a space/comma separated list of GitHub logins. Each expands to
# the noreply address GitHub gives that account, with or without the numeric
# id prefix. An empty list disables the check — a repo that has not opted in
# must not start failing on a rule it never asked for.
#
# Always allowed on top of the list, because refusing them would fail commits
# no human can re-author:
#   · GitHub itself (`noreply@github.com`) — the committer of every squash
#     and merge performed through the web UI or the API;
#   · app/bot accounts (`…[bot]@users.noreply.github.com`) — dependabot,
#     github-actions and friends.
IDENT_RE=""
if [ -n "$AUTHORS" ]; then
    for login in ${AUTHORS//,/ }; do
        esc_login="$(printf '%s' "$login" | sed 's/[][\.^$*+?(){}|]/\\&/g')"
        IDENT_RE="${IDENT_RE}|^([0-9]+\+)?${esc_login}@users\.noreply\.github\.com$"
    done
    IDENT_RE="${IDENT_RE#|}"
    IDENT_RE="${IDENT_RE}|^noreply@github\.com$|^[0-9]+\+[^@]+\[bot\]@users\.noreply\.github\.com$"
fi

ATTR_FILE="$(mktemp)"; FMT_FILE="$(mktemp)"; SEC_FILE="$(mktemp)"; IDENT_FILE="$(mktemp)"
TRAIL_FILE="$(mktemp)"
trap 'rm -f "$ATTR_FILE" "$FMT_FILE" "$SEC_FILE" "$IDENT_FILE" "$TRAIL_FILE"' EXIT
ATTR_N=0; ATTR_SCANNED=0; FMT_N=0; FMT_SCANNED=0; SEC_N=0; IDENT_N=0; TRAIL_N=0

# Identity trailers, whatever the case. An address here is as permanent as the
# author field, so the same allow-list applies to both. Accounts whose display
# name ends in "[bot]" are exempt: dependabot signs off as
# `<support@github.com>`, which no allow-list of human logins can express.
TRAILER_RE='^[[:space:]]*(co-authored-by|signed-off-by|authored-by|committed-by|assisted-by|reviewed-by|acked-by|tested-by|reported-by|suggested-by)[[:space:]]*:'


# --- 1. Attribution ---------------------------------------------------------
# %x1f separates fields, %x1e ends the record: a body is arbitrary multi-line
# text, so a line-oriented read would split it. The record separator keeps
# each message whole whatever it contains.
# `--branches` is the whole-repository audit (rewrite-history.sh); it must
# cover tags too. A tag can hold commits no branch reaches any more — 171 of
# them on one fleet repo, carrying a personal identity that every
# branch-only scan called clean. In CI the scope is a SHA and unaffected.
if [ "$HISTORY" = "full" ]; then
    if [ "$HEAD_REV" = "--branches" ]; then
        # --remotes as well as --branches: a mirror keeps branches in
        # refs/heads, but an actions/checkout working copy has exactly one
        # local branch and puts the rest under refs/remotes/origin. Asking for
        # both makes the same invocation mean "everything this clone knows"
        # in either shape; the unused half is simply empty.
        SCOPE_ARGS=(--branches --tags --remotes)
    else
        SCOPE_ARGS=("$HEAD_REV")
    fi
else
    SCOPE_ARGS=("$RANGE")
fi
ATTR_SCOPE="${SCOPE_ARGS[*]}"

RAW="$(git log "${SCOPE_ARGS[@]}" --format='%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%s%x1f%B%x1e' 2>/dev/null)" || {
    echo "::error::git log failed for '$ATTR_SCOPE' — shallow checkout? (needs fetch-depth: 0)" >&2
    exit 2
}

while IFS= read -r -d $'\x1e' REC; do
    REC="${REC#$'\n'}"
    SHA="${REC%%$'\x1f'*}"; [ -z "$SHA" ] && continue
    REST="${REC#*$'\x1f'}"
    AN="${REST%%$'\x1f'*}"; REST="${REST#*$'\x1f'}"
    AE="${REST%%$'\x1f'*}"; REST="${REST#*$'\x1f'}"
    CN="${REST%%$'\x1f'*}"; REST="${REST#*$'\x1f'}"
    CE="${REST%%$'\x1f'*}"; REST="${REST#*$'\x1f'}"
    SUBJ="${REST%%$'\x1f'*}"; BODY="${REST#*$'\x1f'}"
    ATTR_SCANNED=$((ATTR_SCANNED + 1))

    if [ -n "$IDENT_RE" ]; then
        BAD=""
        printf '%s' "$AE" | grep -qiE -- "$IDENT_RE" || BAD="author $AN <$AE>"
        if ! printf '%s' "$CE" | grep -qiE -- "$IDENT_RE"; then
            [ -n "$BAD" ] && BAD="$BAD; "
            BAD="${BAD}committer $CN <$CE>"
        fi
        if [ -n "$BAD" ]; then
            IDENT_N=$((IDENT_N + 1))
            [ "$IDENT_N" -le "$MAX_REPORT" ] && printf '%s\t%s\t%s\n' "$SHA" "$SUBJ" "$BAD" >> "$IDENT_FILE"
        fi
    fi

    if [ -n "$IDENT_RE" ]; then
        BADT=""
        while IFS= read -r TL; do
            [ -n "$TL" ] || continue
            TEMAIL="$(printf '%s' "$TL" | sed -n 's/.*<\([^>]*\)>.*/\1/p')"
            TNAME="$(printf '%s' "$TL" | sed -n 's/^[^:]*:[[:space:]]*\(.*\)<.*/\1/p' | sed 's/[[:space:]]*$//')"
            printf '%s' "$TNAME" | grep -qiE '\[bot\]$' && continue
            [ -n "$TEMAIL" ] && printf '%s' "$TEMAIL" | grep -qiE -- "$IDENT_RE" && continue
            BADT="${BADT:+$BADT; }$(printf '%s' "$TL" | sed 's/^[[:space:]]*//' | cut -c1-100)"
        done < <(printf '%s' "$BODY" | grep -iE -- "$TRAILER_RE" 2>/dev/null)
        if [ -n "$BADT" ]; then
            TRAIL_N=$((TRAIL_N + 1))
            [ "$TRAIL_N" -le "$MAX_REPORT" ] && printf '%s\t%s\t%s\n' "$SHA" "$SUBJ" "$BADT" >> "$TRAIL_FILE"
        fi
    fi

    HAY="$BODY"$'\n'"author: $AN <$AE>"$'\n'"committer: $CN <$CE>"
    for pattern in "${PATTERNS[@]}"; do
        if printf '%s' "$HAY" | grep -qiE -- "$pattern"; then
            ATTR_N=$((ATTR_N + 1))
            if [ "$ATTR_N" -le "$MAX_REPORT" ]; then
                MATCH="$(printf '%s' "$HAY" | grep -iE -- "$pattern" | head -1 | cut -c1-120)"
                printf '%s\t%s\t%s\t%s\n' "$SHA" "$SUBJ" "$pattern" "$MATCH" >> "$ATTR_FILE"
            fi
            break
        fi
    done
done <<< "$RAW"

# --- 2. Format (range, non-merge) -------------------------------------------
# Conventional Commits: type(scope)!: subject. Merge commits are git-authored
# and skipped; `Revert "…"` is git's own revert shape; "Initial commit" is
# what GitHub writes when a repo is created from a template.
CONVENTIONAL='^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([^)]+\))?!?: [^[:space:]].*$'
GIT_NATIVE='^(Revert "|Initial commit$)'
if [ "$FORMAT" = "true" ] && [ -n "$RANGE" ]; then
    while IFS=$'\x1f' read -r SHA SUBJ; do
        [ -z "$SHA" ] && continue
        FMT_SCANNED=$((FMT_SCANNED + 1))
        if [[ ! "$SUBJ" =~ $CONVENTIONAL ]] && [[ ! "$SUBJ" =~ $GIT_NATIVE ]]; then
            FMT_N=$((FMT_N + 1))
            printf '%s\t%s\n' "$SHA" "$SUBJ" >> "$FMT_FILE"
        fi
    done < <(git log --no-merges --format='%H%x1f%s' "$RANGE" 2>/dev/null)
fi

# --- 3. Secrets (range, added lines only) -----------------------------------
# Only lines the range ADDS: a change that removes a leaked key must not be
# failed by the key it removes. `\+` is a GNU-BRE-only quantifier that ugrep
# rejects outright, so both filters use ERE where `\+` is an unambiguous
# literal plus.
SECRET_RE='password[[:space:]]*[=:][[:space:]]*["'"'"'][^"'"'"']{4,}|api[_-]?key[[:space:]]*[=:][[:space:]]*["'"'"'][^"'"'"']{8,}|secret[_-]?key[[:space:]]*[=:][[:space:]]*["'"'"'][^"'"'"']{8,}|BEGIN (RSA|OPENSSH|DSA|EC|PGP) PRIVATE KEY|ghp_[a-zA-Z0-9]{36}|gho_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{22,}|sk-[a-zA-Z0-9]{48}|AKIA[0-9A-Z]{16}|xox[baprs]-[a-zA-Z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}'
# Test and fixture paths are excluded: their legitimate content includes
# fake credentials that exercise scanners (this repo's own tests/run.sh
# tripped the gate on its first dogfood run). Narrow by design — a real
# secret misplaced under tests/ slips this check; GitGuardian, which runs
# on these repos, does not have that exclusion.
SECRET_EXCLUDE=(':(exclude,glob)tests/**' ':(exclude,glob)**/tests/**'
                ':(exclude,glob)test/**' ':(exclude,glob)**/test/**'
                ':(exclude,glob)testdata/**' ':(exclude,glob)**/testdata/**'
                ':(exclude,glob)fixtures/**' ':(exclude,glob)**/fixtures/**'
                ':(exclude,glob)__tests__/**' ':(exclude,glob)**/__tests__/**'
                ':(exclude,glob)**/*.bats' ':(exclude,glob)**/*_test.*' ':(exclude,glob)**/*.test.*')
if [ "$SECRETS" = "true" ] && [ -n "$RANGE" ]; then
    while IFS= read -r hit; do
        [ -z "$hit" ] && continue
        SEC_N=$((SEC_N + 1))
        printf '%s\n' "$hit" >> "$SEC_FILE"
    done < <(git diff --unified=0 "$RANGE" -- . "${SECRET_EXCLUDE[@]}" 2>/dev/null \
             | grep -E '^\+' | grep -Ev '^\+\+\+' | grep -iE -- "$SECRET_RE" | head -40)
fi

# --- Report -----------------------------------------------------------------
esc() { printf '%s' "$1" | sed 's/|/\\|/g; s/`/ʼ/g'; }
{
    echo "## post-commit"
    echo ""
    if [ "$HISTORY" = "full" ]; then
        echo "Attribution: **$ATTR_SCANNED** commit(s) in the full history of \`${HEAD_REV:0:12}\`."
    else
        echo "Attribution: **$ATTR_SCANNED** commit(s) in \`$RANGE\`."
    fi
    [ -n "$RANGE" ] && echo "Format: **$FMT_SCANNED** non-merge commit(s) in \`$RANGE\`. Secrets: added lines in \`$RANGE\`."
    echo ""
    if [ "$ATTR_N" -eq 0 ] && [ "$FMT_N" -eq 0 ] && [ "$SEC_N" -eq 0 ] && [ "$IDENT_N" -eq 0 ] && [ "$TRAIL_N" -eq 0 ]; then
        echo "✅ Clean — allowed identities, no AI attribution, conventional subjects, no credentials added."
    fi
    if [ "$TRAIL_N" -gt 0 ]; then
        echo "### ❌ Foreign identity in a trailer — $TRAIL_N commit(s)"
        echo ""
        echo "A \`Co-authored-by:\` or \`Signed-off-by:\` line carries an identity as"
        echo "permanently as the author field. Allowed: \`${AUTHORS}\` (GitHub noreply"
        echo "addresses), plus app/bot accounts."
        echo ""
        echo "| Commit | Subject | Trailer |"
        echo "|---|---|---|"
        while IFS=$'\t' read -r sha subj who; do
            printf '| `%s` | %s | %s |\n' "${sha:0:8}" "$(esc "$subj")" "$(esc "$who")"
        done < "$TRAIL_FILE"
        [ "$TRAIL_N" -gt "$MAX_REPORT" ] && echo "" && echo "_… and $((TRAIL_N - MAX_REPORT)) more._"
        [ "$HISTORY" = "full" ] && echo "" && echo "> Existing commits need \`scripts/rewrite-history.sh\`."
        echo ""
    fi
    if [ "$IDENT_N" -gt 0 ]; then
        echo "### ❌ Foreign identity — $IDENT_N commit(s)"
        echo ""
        echo "Allowed: \`${AUTHORS}\` (GitHub noreply addresses), plus GitHub's own"
        echo "merge committer and app/bot accounts."
        echo ""
        echo "| Commit | Subject | Identity |"
        echo "|---|---|---|"
        while IFS=$'\t' read -r sha subj who; do
            printf '| `%s` | %s | %s |\n' "${sha:0:8}" "$(esc "$subj")" "$(esc "$who")"
        done < "$IDENT_FILE"
        [ "$IDENT_N" -gt "$MAX_REPORT" ] && echo "" && echo "_… and $((IDENT_N - MAX_REPORT)) more._"
        echo ""
        echo "> Set your repository identity to the owning account before committing:"
        echo "> \`git config user.email <id>+<login>@users.noreply.github.com\`."
        [ "$HISTORY" = "full" ] && echo "> Existing commits need \`scripts/rewrite-history.sh\`."
        echo ""
    fi
    if [ "$ATTR_N" -gt 0 ]; then
        echo "### ❌ AI attribution — $ATTR_N commit(s)"
        echo ""
        echo "| Commit | Subject | Matched |"
        echo "|---|---|---|"
        while IFS=$'\t' read -r sha subj _pat match; do
            printf '| `%s` | %s | `%s` |\n' "${sha:0:8}" "$(esc "$subj")" "$(esc "$match")"
        done < "$ATTR_FILE"
        [ "$ATTR_N" -gt "$MAX_REPORT" ] && echo "" && echo "_… and $((ATTR_N - MAX_REPORT)) more._"
        if [ "$HISTORY" = "full" ]; then
            echo ""
            echo "> **This branch cannot be merged until the repository's history is rewritten.**"
            echo "> The gate scans every ancestor of the head, so a tainted commit anywhere"
            echo "> in history fails every future PR. Nothing is rewritten automatically —"
            echo "> see \`scripts/rewrite-history.sh\` in kodflow/post-commit."
        fi
        echo ""
    fi
    if [ "$FMT_N" -gt 0 ]; then
        echo "### ❌ Non-conventional subject — $FMT_N commit(s)"
        echo ""
        echo "Expected \`type(scope)!: subject\` with type in feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert."
        echo ""
        while IFS=$'\t' read -r sha subj; do printf -- '- `%s` %s\n' "${sha:0:8}" "$(esc "$subj")"; done < "$FMT_FILE"
        echo ""
    fi
    if [ "$SEC_N" -gt 0 ]; then
        echo "### ❌ Possible credentials — $SEC_N added line(s)"
        echo ""
        echo "Values truncated; check the diff."
        echo ""
        while IFS= read -r l; do printf -- '- `%s…`\n' "$(printf '%s' "$l" | cut -c1-40 | tr -d '`')"; done < "$SEC_FILE"
        echo ""
    fi
} >> "$SUMMARY"

RC=0
if [ "$IDENT_N" -gt 0 ]; then
    RC=1
    while IFS=$'\t' read -r sha subj who; do
        echo "::error::foreign identity in ${sha:0:8} (${subj}) — ${who}"
    done < "$IDENT_FILE"
    echo "::error::$IDENT_N commit(s) not authored by an allowed account (${AUTHORS})"
fi
if [ "$TRAIL_N" -gt 0 ]; then
    RC=1
    while IFS=$'\t' read -r sha subj who; do
        echo "::error::foreign identity in a trailer of ${sha:0:8} (${subj}) — ${who}"
    done < "$TRAIL_FILE"
    echo "::error::$TRAIL_N commit(s) carry a trailer identity outside the allowed accounts (${AUTHORS})"
fi
if [ "$ATTR_N" -gt 0 ]; then
    RC=1
    while IFS=$'\t' read -r sha subj pat match; do
        echo "::error::AI attribution in ${sha:0:8} (${subj}) — matched '${match}' [${pat}]"
    done < "$ATTR_FILE"
    [ "$HISTORY" = "full" ] && echo "::error::$ATTR_N tainted commit(s) in history — blocked until the history is rewritten"
fi
if [ "$FMT_N" -gt 0 ]; then
    RC=1
    while IFS=$'\t' read -r sha subj; do
        echo "::error::non-conventional subject in ${sha:0:8}: ${subj}"
    done < "$FMT_FILE"
fi
if [ "$SEC_N" -gt 0 ]; then
    RC=1
    echo "::error::$SEC_N added line(s) look like credentials"
fi

if [ "$RC" -eq 0 ]; then
    echo "✅ post-commit: $ATTR_SCANNED commit(s) attribution-free${IDENT_RE:+ and correctly attributed}, $FMT_SCANNED subject(s) conventional, no credentials added"
fi
exit "$RC"
