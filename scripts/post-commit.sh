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
#   6. artefacts   — nothing an AI agent WROTE is TRACKED in the tree at the
#                    head: session logs, chat transcripts, plan files, locks,
#                    caches, the credentials store, personal overrides. Its
#                    CONFIGURATION is source and is never matched —
#                    .claude/agents/, commands/, skills/, settings.json,
#                    .cursorrules (`.mcp.json` is check 7's business). The
#                    tree and not the range: an
#                    artefact merged before the rule existed would otherwise
#                    leave every later pull request clean while it sat there.
#   7. mcp.json    — no commit of the range ADDS or MODIFIES a file named
#                    `mcp.json` or `.mcp.json`, at any path. Those files carry
#                    MCP server definitions, and a server definition is where
#                    a token ends up. Not switchable. A deletion, or a rename
#                    to another name, is the way out and always passes.
#
# Foreign-history mode, for a repository listed in foreign-history.txt (a
# fork of someone else's project carrying its whole upstream history):
#   · a commit is OURS when its author or committer is one of PC_AUTHORS,
#     FOREIGN otherwise;
#   · checks 1 and 2 give way to a deny list (denied-identities.txt): no
#     denied address anywhere in any commit, ours or foreign;
#   · check 3 reads our commits whole and a foreign commit's author and
#     committer only, so an assistant committing under its own name is still
#     refused while upstream's messages are not ours to police;
#   · checks 4, 5 and 7 read our commits only; check 6 is unchanged.
# Every other repository is judged exactly as before.
#
# Deliberately NOT here: lint/build/test. Every repo's own CI already runs
# those server-side, so --no-verify never bypassed them in the first place.
#
# usage: post-commit.sh <head-rev> [<range>]
#   head-rev  commit whose whole ancestry is scanned when PC_HISTORY=full
#   range     A..B — scope of format/secrets/mcp.json, and of attribution when
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
AGENT_FILES="${PC_AGENT_FILES:-true}"
# read -a, not an unquoted expansion. `for x in ${VAR//,/ }` word-splits AND
# glob-expands, so an entry like `.claude/*` would be expanded against the
# working tree before it is ever used as a pattern — and bash's filename
# globbing skips leading dots, so `.claude/.mcp.json` would come out NOT
# exempted while `.claude/settings.json` would. read does the splitting
# without the expansion, leaving the entry to be matched as a pattern by
# [[ == ]], where `*` does cross both `/` and a leading dot.
IFS=', ' read -r -a AGENT_ALLOW <<< "${PC_AGENT_ALLOW:-}"
MAX_REPORT="${PC_MAX_REPORT:-50}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"
# Second sink for the same markdown. The job summary is only read by
# someone who opens the run; a caller that wants the verdict where the
# author will actually see it — a pull-request comment — points
# PC_REPORT at a file and reads it back.
REPORT="${PC_REPORT:-}"

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
# the runner, instead of being baked into each pattern. The reader is
# patterns.sh, shared with every other consumer of these files; the gate reads
# commits, so it takes the `commit` scope.
# shellcheck source=scripts/patterns.sh
. "$SCRIPT_DIR/patterns.sh"
PATTERNS=()
AGENT_PATTERNS=()
load_patterns() {   # load_patterns <file> [<array-name>] [<scope>]
    pc_load_patterns "$1" "${2:-PATTERNS}" "${3:-}" || exit 2
}
load_patterns "$SCRIPT_DIR/patterns.txt" PATTERNS commit
[ "$STRICT" = "true" ] && load_patterns "$SCRIPT_DIR/patterns-strict.txt" PATTERNS commit
if [ "${#PATTERNS[@]}" -eq 0 ]; then
    echo "::error::no patterns loaded — refusing to report a false clean" >&2
    exit 2
fi
# Path patterns are a separate list, matched against tracked paths rather than
# against message text — the two never share a pattern, so they never share a
# file either. Empty is treated as an error for the same reason as above: a
# check that silently matches nothing reports a clean it never verified.
if [ "$AGENT_FILES" = "true" ]; then
    load_patterns "$SCRIPT_DIR/agent-paths.txt" AGENT_PATTERNS
    if [ "${#AGENT_PATTERNS[@]}" -eq 0 ]; then
        echo "::error::no agent path patterns loaded — refusing to report a false clean" >&2
        exit 2
    fi
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
IDENT_RE=""; LOGIN_RE=""
if [ -n "$AUTHORS" ]; then
    for login in ${AUTHORS//,/ }; do
        esc_login="$(printf '%s' "$login" | sed 's/[][\.^$*+?(){}|]/\\&/g')"
        IDENT_RE="${IDENT_RE}|^([0-9]+\+)?${esc_login}@users\.noreply\.github\.com$"
    done
    IDENT_RE="${IDENT_RE#|}"
    # Before the generic families are appended: foreign-history mode needs
    # the fleet's own logins alone (see below).
    LOGIN_RE="$IDENT_RE"
    IDENT_RE="${IDENT_RE}|^noreply@github\.com$|^[0-9]+\+[^@]+\[bot\]@users\.noreply\.github\.com$"
fi

# --- Foreign-history mode -----------------------------------------------------
# For a fork that carries its upstream's history: tens of thousands of commits
# by people who are not the fleet, which no allow-list can pass and no message
# rule is ours to impose on. The repository is named in a list that lives in
# THIS repository, never in the caller's: a stub, a variable or a job `env:`
# that could switch the mode on would be a way out of the gate one edit away.
# The action passes both paths explicitly, so a caller's environment cannot
# point them elsewhere either.
#
# OURS is decided by the fleet's logins only (LOGIN_RE), never by GitHub's
# merge committer or an app account: Gitea, Forgejo's ancestor, was merged
# through GitHub's web UI for years, so `noreply@github.com` commits tens of
# thousands of upstream changes.
FOREIGN=false
REPOSITORY="${PC_REPOSITORY:-}"
FOREIGN_REGISTRY="${PC_FOREIGN_REGISTRY:-$SCRIPT_DIR/foreign-history.txt}"
DENY_FILE="${PC_DENY_FILE:-$SCRIPT_DIR/denied-identities.txt}"
if [ -n "$REPOSITORY" ] && [ -f "$FOREIGN_REGISTRY" ]; then
    while IFS= read -r line; do
        line="${line%%#*}"; line="${line//[[:space:]]/}"
        [ -n "$line" ] && [ "${line,,}" = "${REPOSITORY,,}" ] && FOREIGN=true && break
    done < "$FOREIGN_REGISTRY"
fi
declare -A DENY_HASH=()
if [ "$FOREIGN" = "true" ]; then
    if [ -z "$AUTHORS" ]; then
        echo "::error::$REPOSITORY is a foreign-history repository but no authors are set — nothing would tell its commits from upstream's" >&2
        exit 2
    fi
    if [ -f "$DENY_FILE" ]; then
        while IFS= read -r line; do
            line="${line%%#*}"; line="${line//[[:space:]]/}"
            [ -z "$line" ] && continue
            if [[ ! "$line" =~ ^[0-9a-f]{64}$ ]]; then
                echo "::error::$DENY_FILE: not a lowercase SHA-256: '$line'" >&2
                exit 2
            fi
            DENY_HASH["$line"]=1
        done < "$DENY_FILE"
    fi
    # The deny list is all that stands between a personal address and a
    # foreign commit, so an empty one is a check that verifies nothing.
    if [ "${#DENY_HASH[@]}" -eq 0 ]; then
        echo "::error::no denied identity loaded from '$DENY_FILE' — refusing to report a false clean" >&2
        exit 2
    fi
    echo "::notice::foreign-history mode ($REPOSITORY): commits by none of '$AUTHORS' are checked for denied identities and AI authorship only"
fi

# sha256_lines: "<digest>\t<line>" for every line of stdin, in one process
# when perl can (Digest::SHA is core perl; a fork's history holds thousands of
# distinct addresses, and a process per address is seconds of overhead).
sha256_lines() {
    if perl -MDigest::SHA -e1 2>/dev/null; then
        perl -MDigest::SHA=sha256_hex -ne 'chomp; print sha256_hex($_), "\t", $_, "\n"'
        return
    fi
    local l h
    while IFS= read -r l; do
        if command -v sha256sum >/dev/null 2>&1; then h="$(printf '%s' "$l" | sha256sum)"
        else h="$(printf '%s' "$l" | shasum -a 256)"; fi
        printf '%s\t%s\n' "${h%% *}" "$l"
    done
}

ATTR_FILE="$(mktemp)"; FMT_FILE="$(mktemp)"; SEC_FILE="$(mktemp)"; IDENT_FILE="$(mktemp)"
TRAIL_FILE="$(mktemp)"; AGENT_FILE="$(mktemp)"; MCP_FILE="$(mktemp)"; REPORT_BODY="$(mktemp)"
DENY_OUT="$(mktemp)"; FOREIGN_IDS="$(mktemp)"; PAT_FILE="$(mktemp)"
trap 'rm -f "$ATTR_FILE" "$FMT_FILE" "$SEC_FILE" "$IDENT_FILE" "$TRAIL_FILE" "$AGENT_FILE" "$MCP_FILE" "$REPORT_BODY" "$DENY_OUT" "$FOREIGN_IDS" "$PAT_FILE"' EXIT
ATTR_N=0; ATTR_SCANNED=0; FMT_N=0; FMT_SCANNED=0; SEC_N=0; IDENT_N=0; TRAIL_N=0; AGENT_N=0; MCP_N=0
FOREIGN_N=0; DENY_N=0
AGENT_ROOT_N=0

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

RAW_FORMAT='%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%s%x1f%B%x1e'
if [ "$FOREIGN" != "true" ]; then
    RAW="$(git log "${SCOPE_ARGS[@]}" --format="$RAW_FORMAT" 2>/dev/null)" || {
        echo "::error::git log failed for '$ATTR_SCOPE' — shallow checkout? (needs fetch-depth: 0)" >&2
        exit 2
    }
else
    # Sort first, on identities alone: the loop below costs a handful of
    # processes per commit, which over an upstream's 26 000 commits is far
    # beyond the job's timeout. Only OUR commits go through it; a foreign one
    # costs a regex match here and a line in two bulk scans.
    IDS="$(git log "${SCOPE_ARGS[@]}" --format='%H%x1f%an%x1f%ae%x1f%cn%x1f%ce' 2>/dev/null)" || {
        echo "::error::git log failed for '$ATTR_SCOPE' — shallow checkout? (needs fetch-depth: 0)" >&2
        exit 2
    }
    LOGIN_LC="${LOGIN_RE,,}"
    declare -A OWNED=()
    OWNED_LIST=()
    while IFS=$'\x1f' read -r SHA AN AE CN CE; do
        [ -z "$SHA" ] && continue
        if [[ "${AE,,}" =~ $LOGIN_LC || "${CE,,}" =~ $LOGIN_LC ]]; then
            OWNED["$SHA"]=1; OWNED_LIST+=("$SHA")
        else
            FOREIGN_N=$((FOREIGN_N + 1))
            # The identity is the one part of a foreign commit still read for
            # attribution: an assistant committing as itself, under its own
            # name and address, must not pass for upstream.
            printf 'author: %s <%s>\x1f%s\n' "$AN" "$AE" "$SHA" >> "$FOREIGN_IDS"
            printf 'committer: %s <%s>\x1f%s\n' "$CN" "$CE" "$SHA" >> "$FOREIGN_IDS"
        fi
    done <<< "$IDS"
    RAW=""
    if [ "${#OWNED_LIST[@]}" -gt 0 ]; then
        RAW="$(printf '%s\n' "${OWNED_LIST[@]}" | git log --no-walk=unsorted --stdin --format="$RAW_FORMAT" 2>/dev/null)" || {
            echo "::error::git log failed on the repository's own commits" >&2
            exit 2
        }
    fi

    # Foreign identities against the attribution patterns, in one grep. Each
    # line ends with its SHA behind a \x1f, which no pattern can reach into:
    # a SHA is hex, and every pattern needs a letter past `f` or a symbol.
    printf '%s\n' "${PATTERNS[@]}" > "$PAT_FILE"
    declare -A FOREIGN_HIT=()
    while IFS= read -r hit; do
        [ -z "$hit" ] && continue
        SHA="${hit##*$'\x1f'}"; WHO="${hit%$'\x1f'*}"
        [ -n "${FOREIGN_HIT[$SHA]+set}" ] && continue
        FOREIGN_HIT["$SHA"]=1
        ATTR_N=$((ATTR_N + 1))
        if [ "$ATTR_N" -le "$MAX_REPORT" ]; then
            SUBJ="$(git log -1 --format=%s "$SHA")"
            printf '%s\t%s\t%s\t%s\n' "$SHA" "$SUBJ" "identity" "$(printf '%s' "$WHO" | cut -c1-120)" >> "$ATTR_FILE"
        fi
    done < <(grep -iE -f "$PAT_FILE" -- "$FOREIGN_IDS" 2>/dev/null)
    ATTR_SCANNED=$((ATTR_SCANNED + FOREIGN_N))

    # The deny list, over every commit of the scope, ours and foreign alike,
    # and over the whole message, not only the trailers: an address the owner
    # keeps out of histories is out of all of it. Every address-shaped token
    # is collected once, hashed with and without its local part, and only a
    # match sends the history through a second, locating pass.
    declare -A DENIED=()
    CANDS="$(git log "${SCOPE_ARGS[@]}" --format='%ae%n%ce%n%B' 2>/dev/null \
              | grep -oE '[[:alnum:]._%+-]+@[[:alnum:].-]+' | tr '[:upper:]' '[:lower:]' \
              | sed 's/\.*$//' | sort -u)"
    if [ -n "$CANDS" ]; then
        declare -A DIGEST=()
        while IFS=$'\t' read -r h v; do DIGEST["$v"]="$h"; done < <(
            { printf '%s\n' "$CANDS"; printf '%s\n' "$CANDS" | sed 's/^[^@]*//'; } | sort -u | sha256_lines)
        while IFS= read -r e; do
            [ -z "$e" ] && continue
            if [ -n "${DENY_HASH[${DIGEST[$e]}]+set}" ] || [ -n "${DENY_HASH[${DIGEST[@${e#*@}]}]+set}" ]; then
                DENIED["$e"]=1
            fi
        done <<< "$CANDS"
    fi
    if [ "${#DENIED[@]}" -gt 0 ]; then
        # Where it sits, never what it is: the report is a job summary and a
        # pull-request comment, and printing the address would publish the
        # very thing this check keeps out.
        while IFS=$'\t' read -r SHA SUBJ WHERE; do
            [ -z "$SHA" ] && continue
            DENY_N=$((DENY_N + 1))
            [ "$DENY_N" -le "$MAX_REPORT" ] && printf '%s\t%s\t%s\n' "$SHA" "$SUBJ" "$WHERE" >> "$DENY_OUT"
        done < <(git log "${SCOPE_ARGS[@]}" --format='%H%x1f%s%x1f%ae%x1f%ce%x1f%B%x1e' 2>/dev/null \
                 | PC_DENIED="$(printf '%s\n' "${!DENIED[@]}")" awk -v RS=$'\x1e' -v FS=$'\x1f' '
                     BEGIN { n = split(ENVIRON["PC_DENIED"], d, "\n") }
                     {
                         sub(/^\n+/, "")
                         if ($0 == "") next
                         where = ""
                         for (i = 1; i <= n; i++) {
                             if (d[i] == "") continue
                             if (index(tolower($3), d[i]) && where !~ /author/)    where = where (where ? ", " : "") "author"
                             if (index(tolower($4), d[i]) && where !~ /committer/) where = where (where ? ", " : "") "committer"
                             if (index(tolower($5), d[i]) && where !~ /message/)   where = where (where ? ", " : "") "message"
                         }
                         if (where != "") { gsub(/\t/, " ", $2); print $1 "\t" $2 "\t" where }
                     }')
    fi
fi

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

    # Foreign-history mode: the deny list above decides who may appear.
    if [ -n "$IDENT_RE" ] && [ "$FOREIGN" != "true" ]; then
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

    if [ -n "$IDENT_RE" ] && [ "$FOREIGN" != "true" ]; then
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
# ours <sha>: true outside foreign-history mode, and there for OUR commits
# only. Format, secrets and mcp.json judge what the fleet writes; upstream's
# subjects and diffs follow upstream's rules, not this repository's.
ours() { [ "$FOREIGN" != "true" ] || [ -n "${OWNED[$1]+set}" ]; }
if [ "$FORMAT" = "true" ] && [ -n "$RANGE" ]; then
    while IFS=$'\x1f' read -r SHA SUBJ; do
        [ -z "$SHA" ] && continue
        ours "$SHA" || continue
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
# Split in two halves, because only one of them can ever be a reference.
#
# The KEYWORD half is `password = "…"` and friends: a name and a value, where
# the value may legitimately be a substitution. That half is subject to the
# exemption below.
#
# The TOKEN half is a credential's own printed form — an AWS key id, a GitHub
# PAT, a PEM header. No substitution produces one of those, so that half is
# NEVER exempted, and it is scanned separately. Keeping them together is what
# let a line carrying both a lookup and a real token be dropped wholesale.
SECRET_KEYWORD_RE='password[[:space:]]*[=:][[:space:]]*["'"'"'][^"'"'"']{4,}|api[_-]?key[[:space:]]*[=:][[:space:]]*["'"'"'][^"'"'"']{8,}|secret[_-]?key[[:space:]]*[=:][[:space:]]*["'"'"'][^"'"'"']{8,}'
SECRET_TOKEN_RE='BEGIN (RSA|OPENSSH|DSA|EC|PGP) PRIVATE KEY|ghp_[a-zA-Z0-9]{36}|gho_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{22,}|sk-[a-zA-Z0-9]{48}|AKIA[0-9A-Z]{16}|xox[baprs]-[a-zA-Z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}'
# A value that is a SUBSTITUTION is not a credential. A jinja `{{ … }}`, a
# shell `${…}`, an ERB `<%= … %>` name WHERE the secret comes from — they are
# the pattern this gate exists to encourage, and failing them teaches people to
# hide the reference rather than the secret.
#
# Filtered in a second pass rather than by loosening SECRET_RE. Loosening it
# would mean "the value must not start with `{`, `$`, `<`" — which also stops
# matching crypt and bcrypt hashes, whose values start with a dollar and which
# very much are credentials.
#
# Only the three keyword shapes need this. The token shapes (AKIA…, ghp_…,
# BEGIN … PRIVATE KEY) cannot be produced by a substitution.
#
# Narrow by design, and worth stating: this drops the whole LINE, so a line
# carrying both a placeholder assignment and a real token would be dropped with
# it. A line of that shape is not a thing anyone writes; GitGuardian, which
# runs on these repos, has no such blind spot either.
# The exempted value is a substitution that is the WHOLE value — opener, body,
# closer, then the closing quote. Anchoring the end matters: without it,
# `"${PASSWORD}hunter2000"` and `"{{ key }}hunter2000"` would be exempted on
# their first three characters and carry a literal out past the gate.
#
# Quotes INSIDE the body are allowed, deliberately. A narrower rule was tried
# first — "a bare reference, no quote inside" — and it refused
# `{{ lookup('pipe', …) }}`, which is how Ansible fetches a secret from an
# external store at run time: the single best thing a config file can do with a
# password, and the exact opposite of a leak. Quotes are not the signal.
SECRET_PLACEHOLDER_RE='(password|api[_-]?key|secret[_-]?key)[[:space:]]*[=:][[:space:]]*["'"'"'][[:space:]]*(\{\{[^{}]*\}\}|\{%[^{}]*%\}|\$\{[a-z_][a-z0-9_]*\}|\$\([^()]*\)|<%=[^<>]*%>|%\{[^{}]*\})[[:space:]]*["'"'"']'
# The dangerous subset: a fallback that supplies a LITERAL.
# `${API_KEY:-hunter2000}` and `{{ key | default('hunter2000') }}` commit the
# very secret the reference was supposed to keep out, so they are added back as
# hits below.
#
# What makes it a literal is the argument, not the `default` itself. A default
# is how you write an OPTIONAL reference — `default(omit)` is the ordinary
# Ansible idiom for "leave this out", and `default(vault_password)` names
# another variable. Neither commits anything. So the argument must open with a
# quote or a digit, and a shell fallback must not be empty or another `$`.
SECRET_FALLBACK_RE='(password|api[_-]?key|secret[_-]?key)[[:space:]]*[=:][[:space:]]*["'"'"'][[:space:]]*(\{\{[^{}]*\|[[:space:]]*d(efault)?[[:space:]]*\([[:space:]]*["'"'"'0-9]|\$\{[a-z_][a-z0-9_]*:?[-=?+][^}$])'
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
# The added lines of the range, read three times below. Three passes rather
# than one because the rule is a union whose parts need opposite senses, which
# a single grep cannot express:
#
#   1. a credential's own printed form  — never exempt
#   2. a keyword assignment that is NOT a substitution
#   3. a substitution whose fallback supplies a literal
#
# Pass 1 is separate for a reason: the exemption drops a whole LINE, so folding
# tokens in with keywords meant a line carrying both a lookup and a real PAT was
# discarded with the lookup. Scanned on its own, the token is always found.
#
# The passes overlap — a `${VAR:-secret}` line is in 2 and 3 — so the union is
# de-duplicated. awk rather than `sort -u`: it keeps the diff's own order, which
# is the order the report reads best in.
#
# In foreign-history mode the range is mostly upstream's code, so the added
# lines are read per commit, from OUR non-merge commits only — each against
# its parent. That is stricter for those commits than the range's two ends: a
# credential added in one and removed in the next is still found.
secret_added_lines() {
    if [ "$FOREIGN" = "true" ]; then
        local sha
        while IFS= read -r sha; do
            [ -z "$sha" ] && continue
            ours "$sha" || continue
            git diff-tree -p -r --root --no-commit-id --unified=0 "$sha" -- . "${SECRET_EXCLUDE[@]}" 2>/dev/null
        done < <(git log --no-merges --format=%H "$RANGE" 2>/dev/null) \
          | grep -E '^\+' | grep -Ev '^\+\+\+'
        return
    fi
    git diff --unified=0 "$RANGE" -- . "${SECRET_EXCLUDE[@]}" 2>/dev/null \
      | grep -E '^\+' | grep -Ev '^\+\+\+'
}
if [ "$SECRETS" = "true" ] && [ -n "$RANGE" ]; then
    while IFS= read -r hit; do
        [ -z "$hit" ] && continue
        SEC_N=$((SEC_N + 1))
        printf '%s\n' "$hit" >> "$SEC_FILE"
    done < <( { secret_added_lines | grep -iE -- "$SECRET_TOKEN_RE"; \
                secret_added_lines | grep -iE -- "$SECRET_KEYWORD_RE" \
                                   | grep -ivE -- "$SECRET_PLACEHOLDER_RE"; \
                secret_added_lines | grep -iE -- "$SECRET_FALLBACK_RE"; } \
              | awk '!seen[$0]++' | head -40)
fi

# --- 4. Agent artefacts (the tree at the head) -------------------------------
# What an agent WROTE, never what a human wrote for it. The configuration is
# source — someone authored `.claude/agents/`, reviewed it, and wants the next
# person who clones to have it. The exhaust is not: a session log, a
# transcript, a plan, a lock, a cache, a credentials store, a personal
# override. See agent-paths.txt for where exactly the line falls and for the
# fleet measurements that put it there.
#
# Scope is the tree, not the range, and that is the point. The artefact that
# prompted this rule was merged into a trunk long before the rule existed; a
# range check sees only what a change adds, so every later pull request would
# have been called clean while it sat there.
#
# Which it can afford to be, because gone is cheap here. A tainted commit
# message needs rewrite-history.sh and new SHAs for every descendant; a tracked
# file needs one `git rm -r --cached` and a commit. The check never looks at
# the ancestry, so that commit ends it.
#
# --branches is the whole-repository audit scope (push, manual runs,
# rewrite-history.sh) and is not a tree-ish. There, the checked-out head is the
# tree to judge — on push that is exactly the commit that was pushed.
TREE_REV="$HEAD_REV"; [ "$TREE_REV" = "--branches" ] && TREE_REV=HEAD
declare -A AGENT_ROOT_COUNT=()
AGENT_ROOT_ORDER=()
if [ "$AGENT_FILES" = "true" ]; then
    # One combined alternation rather than a grep per pattern: a large
    # repository has tens of thousands of tracked paths, and forty passes over
    # them is forty times the work for the same answer.
    AGENT_RE=""
    for pattern in "${AGENT_PATTERNS[@]}"; do AGENT_RE="${AGENT_RE}|(${pattern})"; done
    AGENT_RE="${AGENT_RE#|}"

    # -z, and NUL all the way through. Without it git C-quotes any path holding
    # a non-ASCII or control character — `.claude/naïve.md` is emitted as
    # `".claude/na\303\257ve.md"` — and the quote it adds sits exactly where
    # `(^|/)` and `$` need a path boundary, so every pattern in agent-paths.txt
    # stops matching. Measured before the fix: a repository whose `.claude/`
    # files all carried an accent was reported clean. Naming files that way is
    # a one-line evasion of the whole rule.
    #
    # The paths cannot pass through a shell variable on the way, because a bash
    # string cannot hold a NUL. So the pipeline runs straight into the loop and
    # the revision is verified separately rather than through git's exit status.
    git rev-parse -q --verify "${TREE_REV}^{tree}" >/dev/null 2>&1 || {
        echo "::error::no tree at '$TREE_REV' — shallow checkout? (needs fetch-depth: 0)" >&2
        exit 2
    }
    while IFS= read -r -d '' p; do
        [ -z "$p" ] && continue
        ALLOWED=""
        for allow in ${AGENT_ALLOW[@]+"${AGENT_ALLOW[@]}"}; do
            # A bare entry exempts the whole subtree, so `.claude` and
            # `.claude/*` both mean what whoever wrote the input expects. `*`
            # crosses `/` inside [[ == ]], so no globstar is involved.
            # shellcheck disable=SC2053
            if [[ "$p" == $allow || "$p" == $allow/* ]]; then ALLOWED=y; break; fi
        done
        [ -n "$ALLOWED" ] && continue
        AGENT_N=$((AGENT_N + 1))
        printf '%s\0' "$p" >> "$AGENT_FILE"

        # The artefact root: the SHORTEST PREFIX THAT STILL MATCHES. Not the
        # first dot-directory along the path, and the difference is not
        # cosmetic — the fleet's devcontainer ships its agent configuration at
        # `.devcontainer/images/.claude/…`, so collapsing on the first would
        # name `.devcontainer/` as the thing to delete: a directory this gate
        # promises never to touch, holding 400 files it has no quarrel with. A
        # verdict that names the wrong directory is worse than none.
        #
        # Split with parameter expansion rather than `read -a`: read stops at a
        # newline, and a newline is one of the characters a path may contain
        # and this loop exists to handle. Lowercasing the candidate is how the
        # grep's -i is carried into [[ =~ ]]; a pattern written with an
        # uppercase letter simply would not collapse, leaving the whole path
        # reported, which is accurate and merely longer.
        cand=""; rest="$p"; root="$p"
        while [ -n "$rest" ]; do
            seg="${rest%%/*}"
            cand="${cand}${seg}"
            if [ "$seg" = "$rest" ]; then rest=""; else cand="${cand}/"; rest="${rest#*/}"; fi
            if [[ "${cand,,}" =~ $AGENT_RE ]]; then root="$cand"; break; fi
        done
        if [ -z "${AGENT_ROOT_COUNT[$root]+set}" ]; then
            AGENT_ROOT_ORDER+=("$root"); AGENT_ROOT_COUNT["$root"]=0
        fi
        AGENT_ROOT_COUNT["$root"]=$(( AGENT_ROOT_COUNT["$root"] + 1 ))
    done < <(git ls-tree -r -z --name-only "$TREE_REV" | grep -z -iE -- "$AGENT_RE")
    AGENT_ROOT_N="${#AGENT_ROOT_ORDER[@]}"
fi

# --- 5. mcp.json (range, every commit) --------------------------------------
# An MCP server definition is a command line and an environment, and the
# environment is where the token goes. Decided on 2026-10-14: no file named
# `mcp.json` or `.mcp.json` is ever committed, whatever directory it sits in.
#
# EVERY commit of the range, not the diff between its ends. A token added in
# one commit and removed in the next leaves the endpoints identical and the
# token in the history, which is exactly where it must not be. Reading each
# commit is also why the fix is to drop the file FROM the commit that added it
# rather than to add a removal on top.
#
# Only additions and modifications are refused. A deletion is the cleanup, and
# a rename's DESTINATION is what is judged, so `.mcp.json` -> `mcp.json.tmpl`
# passes and `notes.txt` -> `.mcp.json` does not. Merges are skipped like the
# format check skips them: what a merge brings in was added by a commit the
# range already holds.
#
# -z and NUL all the way, as in the artefact check: without it git C-quotes a
# path holding an unusual byte and the `(^|/)` boundary stops matching. The
# whole-repository scope has no range and therefore no mcp.json check, like
# format and secrets: it judges a change, not the archive.
MCP_RE='(^|/)\.?mcp\.json$'
if [ -n "$RANGE" ]; then
    while IFS= read -r SHA; do
        [ -z "$SHA" ] && continue
        ours "$SHA" || continue
        SUBJ="$(git log -1 --format=%s "$SHA")"
        while IFS= read -r -d '' ST; do
            P=""
            case "$ST" in
                R*|C*) IFS= read -r -d '' _old; IFS= read -r -d '' P ;;
                D*)    IFS= read -r -d '' _old; continue ;;
                *)     IFS= read -r -d '' P ;;
            esac
            [[ "${P,,}" =~ $MCP_RE ]] || continue
            MCP_N=$((MCP_N + 1))
            printf '%s\x1f%s\x1f%s\x1f%s\0' "$SHA" "${ST:0:1}" "$P" "$SUBJ" >> "$MCP_FILE"
        done < <(git diff-tree -r --root --no-commit-id --name-status -z -M "$SHA")
    done < <(git log --no-merges --format=%H "$RANGE" 2>/dev/null)
fi

# --- Report -----------------------------------------------------------------
esc() { printf '%s' "$1" | sed 's/|/\\|/g; s/`/ʼ/g'; }
# GitHub workflow-command property values are comma-separated and newline-
# terminated, so a path carrying either would truncate or split the annotation
# it is supposed to point at. Pure parameter expansion: a path is arbitrary
# bytes and piping it through sed would lose the newline this exists to encode.
esc_prop() {
    local v="$1"
    v="${v//%/%25}"; v="${v//$'\r'/%0D}"; v="${v//$'\n'/%0A}"
    v="${v//:/%3A}"; v="${v//,/%2C}"
    printf '%s' "$v"
}
{
    echo "## post-commit"
    echo ""
    if [ "$HISTORY" = "full" ]; then
        echo "Attribution: **$ATTR_SCANNED** commit(s) in the full history of \`${HEAD_REV:0:12}\`."
    else
        echo "Attribution: **$ATTR_SCANNED** commit(s) in \`$RANGE\`."
    fi
    [ -n "$RANGE" ] && echo "Format: **$FMT_SCANNED** non-merge commit(s) in \`$RANGE\`. Secrets: added lines in \`$RANGE\`. mcp.json: every non-merge commit in \`$RANGE\`."
    [ "$AGENT_FILES" = "true" ] && echo "Agent artefacts: paths tracked at \`${TREE_REV:0:12}\`."
    if [ "$FOREIGN" = "true" ]; then
        echo ""
        echo "Foreign-history mode (\`$REPOSITORY\`): **$FOREIGN_N** commit(s) by none of \`${AUTHORS}\` —"
        echo "checked for denied identities and AI authorship only. **$((ATTR_SCANNED - FOREIGN_N))** of the"
        echo "repository's own, checked in full. Denied identities: every commit of the scope."
    fi
    echo ""
    if [ "$ATTR_N" -eq 0 ] && [ "$FMT_N" -eq 0 ] && [ "$SEC_N" -eq 0 ] && [ "$IDENT_N" -eq 0 ] \
       && [ "$TRAIL_N" -eq 0 ] && [ "$AGENT_N" -eq 0 ] && [ "$MCP_N" -eq 0 ] && [ "$DENY_N" -eq 0 ]; then
        # Only a range is read for mcp.json, so only a range may claim it.
        if [ "$FOREIGN" = "true" ]; then
            echo "✅ Clean — no denied identity, no AI attribution, conventional subjects, no credentials added, no agent artefacts tracked${RANGE:+, no mcp.json committed}."
        else
            echo "✅ Clean — allowed identities, no AI attribution, conventional subjects, no credentials added, no agent artefacts tracked${RANGE:+, no mcp.json committed}."
        fi
    fi
    if [ "$DENY_N" -gt 0 ]; then
        echo "### ❌ Denied identity — $DENY_N commit(s)"
        echo ""
        echo "An address on the deny list (\`scripts/denied-identities.txt\` in"
        echo "kodflow/post-commit) appears in these commits. It is not repeated here:"
        echo "this report is public wherever the repository is."
        echo ""
        echo "| Commit | Subject | Where |"
        echo "|---|---|---|"
        while IFS=$'\t' read -r sha subj where; do
            printf '| `%s` | %s | %s |\n' "${sha:0:8}" "$(esc "$subj")" "$(esc "$where")"
        done < "$DENY_OUT"
        [ "$DENY_N" -gt "$MAX_REPORT" ] && echo "" && echo "_… and $((DENY_N - MAX_REPORT)) more._"
        echo ""
        echo "> Rewrite those commits with the owning account's noreply address"
        echo "> (\`scripts/rewrite-history.sh\` for commits already on a branch)."
        echo ""
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
    if [ "$AGENT_N" -gt 0 ]; then
        echo "### ❌ Agent artefacts tracked — $AGENT_N file(s)"
        echo ""
        echo "These are files an agent WROTE — a session log, a transcript, a plan,"
        echo "a lock, a cache, a personal override. Nobody reviews them, they conflict"
        echo "on every merge, and they carry whatever the session happened to touch."
        echo ""
        shown=0
        for root in ${AGENT_ROOT_ORDER[@]+"${AGENT_ROOT_ORDER[@]}"}; do
            shown=$((shown + 1)); [ "$shown" -gt "$MAX_REPORT" ] && break
            # %q on the way out: a no-op for an ordinary path, and the only
            # honest rendering of one holding a newline or a control character
            # — which would otherwise break the list it is printed into.
            disp="$(printf '%q' "$root")"
            if [ "${AGENT_ROOT_COUNT[$root]}" -gt 1 ]; then
                printf -- '- `%s` — %s file(s)\n' "$(esc "$disp")" "${AGENT_ROOT_COUNT[$root]}"
            else
                printf -- '- `%s`\n' "$(esc "$disp")"
            fi
        done
        [ "$AGENT_ROOT_N" -gt "$MAX_REPORT" ] && echo "" && echo "_… and $((AGENT_ROOT_N - MAX_REPORT)) more._"
        echo ""
        echo "> Untrack them and commit — the file stays on your machine:"
        echo "> \`git rm -r --cached <path>\` then add it to \`.gitignore\`."
        echo "> **No history rewrite is needed**: this check reads the tree at the head,"
        echo "> not the ancestry, so one commit clears it."
        echo ">"
        echo "> The agent's **configuration is source and is never matched**:"
        echo "> \`.claude/agents/\`, \`commands/\`, \`skills/\`, \`settings.json\`,"
        echo "> \`.cursorrules\` — and editor config and \`CLAUDE.md\` alongside them."
        echo "> Only the listed runtime directories and filenames are refused, wherever"
        echo "> they sit. \`agent_files_allow\` exempts a path if one is genuinely wanted."
        echo ""
    fi
    if [ "$MCP_N" -gt 0 ]; then
        echo "### ❌ mcp.json committed — $MCP_N change(s)"
        echo ""
        echo "A file named \`mcp.json\` or \`.mcp.json\` holds MCP server definitions,"
        echo "and a server definition is where a token ends up. None is ever committed,"
        echo "at any path. Every commit of the range is read, so a later commit that"
        echo "removes the file does not clear the one that added it."
        echo ""
        echo "| Commit | Subject | Change | Path |"
        echo "|---|---|---|---|"
        shown=0
        while IFS=$'\x1f' read -r -d '' sha st p subj; do
            shown=$((shown + 1)); [ "$shown" -gt "$MAX_REPORT" ] && break
            case "$st" in A) st=added ;; M) st=modified ;; R) st="renamed to" ;; C) st="copied to" ;; *) st=changed ;; esac
            printf '| `%s` | %s | %s | `%s` |\n' "${sha:0:8}" "$(esc "$subj")" "$st" "$(esc "$(printf '%q' "$p")")"
        done < "$MCP_FILE"
        [ "$MCP_N" -gt "$MAX_REPORT" ] && echo "" && echo "_… and $((MCP_N - MAX_REPORT)) more._"
        echo ""
        echo "> Take the file out of the commit that introduced it, then push the branch"
        echo "> again with \`git push --force-with-lease\`:"
        echo "> \`git rm --cached <path>\` and \`git commit --amend\`, or a \`fixup!\` commit and"
        echo "> \`git rebase -i --autosquash\` when it is further back."
        echo "> Keep a shareable version as \`mcp.json.tmpl\`, every token replaced by a"
        echo "> placeholder, and add \`mcp.json\` and \`.mcp.json\` to \`.gitignore\`."
        echo "> **If the file ever held a real token, revoke it**: the push already published it."
        echo ""
    fi
} > "$REPORT_BODY"
cat "$REPORT_BODY" >> "$SUMMARY"
[ -n "$REPORT" ] && cp "$REPORT_BODY" "$REPORT"

RC=0
if [ "$DENY_N" -gt 0 ]; then
    RC=1
    while IFS=$'\t' read -r sha subj where; do
        echo "::error::denied identity in ${sha:0:8} (${subj}) — ${where}"
    done < "$DENY_OUT"
    echo "::error::$DENY_N commit(s) carry an identity on the deny list"
fi
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
if [ "$AGENT_N" -gt 0 ]; then
    RC=1
    # file= makes GitHub anchor the annotation on the offending path itself,
    # which is also the instruction: this is the file to remove.
    shown=0
    while IFS= read -r -d '' a; do
        shown=$((shown + 1)); [ "$shown" -gt "$MAX_REPORT" ] && break
        echo "::error file=$(esc_prop "$a")::agent artefact tracked here — remove it (git rm -r --cached)"
    done < "$AGENT_FILE"
    echo "::error::$AGENT_N agent artefact file(s) tracked at ${TREE_REV:0:12} — no history rewrite needed, one commit removes them"
fi
if [ "$MCP_N" -gt 0 ]; then
    RC=1
    shown=0
    while IFS=$'\x1f' read -r -d '' sha _st p _subj; do
        shown=$((shown + 1)); [ "$shown" -gt "$MAX_REPORT" ] && break
        echo "::error file=$(esc_prop "$p")::mcp.json committed in ${sha:0:8} — drop it from that commit (git rm --cached, then amend or fixup), keep mcp.json.tmpl, gitignore it"
    done < "$MCP_FILE"
    echo "::error::$MCP_N mcp.json addition(s) or modification(s) in $RANGE — never committed, at any path"
fi

if [ "$RC" -eq 0 ]; then
    # PC_AGENT_FILES is "true" or "false" — both non-empty, so the claim is
    # built from whether the check ran, not from the flag's truthiness.
    AGENT_CLAIM=""
    [ "$AGENT_FILES" = "true" ] && AGENT_CLAIM=", no agent artefacts tracked"
    MCP_CLAIM=""
    [ -n "$RANGE" ] && MCP_CLAIM=", no mcp.json committed"
    IDENT_CLAIM="${IDENT_RE:+ and correctly attributed}"
    [ "$FOREIGN" = "true" ] && IDENT_CLAIM=" and free of denied identities ($FOREIGN_N foreign)"
    echo "✅ post-commit: $ATTR_SCANNED commit(s) attribution-free${IDENT_CLAIM}, $FMT_SCANNED subject(s) conventional, no credentials added${AGENT_CLAIM}${MCP_CLAIM}"
fi
exit "$RC"
