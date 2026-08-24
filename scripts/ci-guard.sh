#!/usr/bin/env bash
# ============================================================================
# ci-guard.sh — commit-message / secret guard for CI.
#
# Ported from kodflow/ktn-linter's scripts/git-guard.sh (PR #447, hardened
# over three CodeRabbit rounds). That script is a Claude Code PreToolUse hook:
# it inspects a *git command line* before it runs and can therefore refuse
# --no-verify, rewrite --force into --force-with-lease, and stop the commit
# from ever existing. None of that is observable from CI, which only ever
# sees commits that already exist. What ports cleanly is the part that
# inspects *content*: the AI-attribution patterns and the secret scan. Those
# are what this script implements. See README.md § "What CI cannot do".
#
# Exit 0 = clean, 1 = violations found, 2 = usage/internal error.
# ============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

RANGE="${1:-}"
STRICT="${GUARD_STRICT:-false}"
SCAN_SECRETS="${GUARD_SECRETS:-true}"
MAX_REPORT="${GUARD_MAX_REPORT:-50}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"

if [ -z "$RANGE" ]; then
    echo "usage: ci-guard.sh <git-log-range|--all>" >&2
    exit 2
fi

# --- Load patterns ---------------------------------------------------------
# Strip comments and blank lines. Patterns are EREs consumed by `grep -iE`;
# case-insensitivity is applied here, once, rather than being baked into each
# pattern — the original's case-SENSITIVE patterns silently missed git's own
# default "Co-authored-by:" spelling (ktn-linter PR #447).
PATTERNS=()
load_patterns() {
    local f="$1"
    [ -r "$f" ] || { echo "::error::pattern file not readable: $f" >&2; exit 2; }
    local line
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue ;; esac
        PATTERNS+=("$line")
    done < "$f"
}
load_patterns "$SCRIPT_DIR/patterns.txt"
if [ "$STRICT" = "true" ]; then
    load_patterns "$SCRIPT_DIR/patterns-strict.txt"
fi
if [ "${#PATTERNS[@]}" -eq 0 ]; then
    echo "::error::no patterns loaded — refusing to report a false clean" >&2
    exit 2
fi

# --- Collect commits -------------------------------------------------------
# %x1f separates sha from body, %x1e terminates the record. A commit body is
# arbitrary multi-line text, so a line-oriented read would split it; the
# record separator keeps each message whole regardless of its content.
if [ "$RANGE" = "--all" ]; then
    LOG_ARGS=(--all)
else
    LOG_ARGS=("$RANGE")
fi

RAW="$(git log "${LOG_ARGS[@]}" --format='%H%x1f%an%x1f%ae%x1f%s%x1f%B%x1e' 2>/dev/null)" || {
    echo "::error::git log failed for range '$RANGE' — is the checkout deep enough (fetch-depth: 0)?" >&2
    exit 2
}

VIOLATIONS=0
SCANNED=0
REPORTED=0
REPORT_FILE="$(mktemp)"
trap 'rm -f "$REPORT_FILE"' EXIT

# Read records split on \x1e. The trailing empty record after the final
# separator is skipped by the empty-sha guard.
while IFS= read -r -d $'\x1e' RECORD; do
    RECORD="${RECORD#$'\n'}"
    SHA="${RECORD%%$'\x1f'*}"
    [ -z "$SHA" ] && continue
    REST="${RECORD#*$'\x1f'}"
    AUTHOR="${REST%%$'\x1f'*}"; REST="${REST#*$'\x1f'}"
    EMAIL="${REST%%$'\x1f'*}";  REST="${REST#*$'\x1f'}"
    SUBJECT="${REST%%$'\x1f'*}"; BODY="${REST#*$'\x1f'}"

    SCANNED=$((SCANNED + 1))

    # The author identity is part of the attribution surface: a commit
    # authored as "Claude <noreply@anthropic.com>" carries no trailer at all,
    # so scanning the message alone would pass it.
    HAYSTACK="$BODY"$'\n'"$AUTHOR <$EMAIL>"

    for pattern in "${PATTERNS[@]}"; do
        if printf '%s' "$HAYSTACK" | grep -qiE -- "$pattern"; then
            VIOLATIONS=$((VIOLATIONS + 1))
            if [ "$REPORTED" -lt "$MAX_REPORT" ]; then
                REPORTED=$((REPORTED + 1))
                MATCH="$(printf '%s' "$HAYSTACK" | grep -inE -- "$pattern" | head -1)"
                printf '%s\t%s\t%s\t%s\n' "$SHA" "$SUBJECT" "$pattern" "$MATCH" >> "$REPORT_FILE"
            fi
            break   # one finding per commit is enough to fail it
        fi
    done
done <<< "$RAW"

# --- Secret scan on added lines in the range -------------------------------
SECRET_HITS=0
SECRET_FILE="$(mktemp)"
trap 'rm -f "$REPORT_FILE" "$SECRET_FILE"' EXIT

if [ "$SCAN_SECRETS" = "true" ] && [ "$RANGE" != "--all" ]; then
    # Only ADDED lines (^+, excluding the +++ header) matter: a diff that
    # removes a secret must not fail the PR that removes it.
    while IFS= read -r hit; do
        [ -z "$hit" ] && continue
        SECRET_HITS=$((SECRET_HITS + 1))
        printf '%s\n' "$hit" >> "$SECRET_FILE"
    done < <(
        git diff --unified=0 "$RANGE" -- . 2>/dev/null \
        | grep -E '^\+' | grep -Ev '^\+\+\+' \
        | grep -iE 'password[[:space:]]*=[[:space:]]*["'"'"'][^"'"'"']{4,}|api[_-]?key[[:space:]]*=[[:space:]]*["'"'"'][^"'"'"']{8,}|secret[_-]?key[[:space:]]*=[[:space:]]*["'"'"'][^"'"'"']{8,}|BEGIN (RSA|OPENSSH|DSA|EC|PGP) PRIVATE KEY|ghp_[a-zA-Z0-9]{36}|gho_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{22,}|sk-[a-zA-Z0-9]{48}|AKIA[0-9A-Z]{16}|xox[baprs]-[a-zA-Z0-9-]{10,}' \
        | head -40
    )
fi

# --- Report ----------------------------------------------------------------
{
    echo "## Commit guard"
    echo ""
    echo "Scanned **$SCANNED** commit(s) in \`$RANGE\`."
    echo ""
    if [ "$VIOLATIONS" -eq 0 ] && [ "$SECRET_HITS" -eq 0 ]; then
        echo "✅ No AI attribution or secrets found."
    fi
    if [ "$VIOLATIONS" -gt 0 ]; then
        echo "### ❌ AI attribution — $VIOLATIONS commit(s)"
        echo ""
        echo "| Commit | Subject | Pattern |"
        echo "|---|---|---|"
        while IFS=$'\t' read -r sha subj pat _match; do
            printf '| `%s` | %s | `%s` |\n' "${sha:0:8}" "${subj//|/\\|}" "${pat//|/\\|}"
        done < "$REPORT_FILE"
        if [ "$VIOLATIONS" -gt "$REPORTED" ]; then
            echo ""
            echo "_… and $((VIOLATIONS - REPORTED)) more (capped at \`GUARD_MAX_REPORT=$MAX_REPORT\`)._"
        fi
        echo ""
    fi
    if [ "$SECRET_HITS" -gt 0 ]; then
        echo "### ❌ Possible secrets — $SECRET_HITS added line(s)"
        echo ""
        echo "Values are redacted below; check the diff."
        echo ""
        while IFS= read -r l; do
            printf '-  `%s…`\n' "$(printf '%s' "$l" | cut -c1-40 | tr -d '`')"
        done < "$SECRET_FILE"
        echo ""
    fi
} >> "$SUMMARY"

if [ "$VIOLATIONS" -gt 0 ] || [ "$SECRET_HITS" -gt 0 ]; then
    while IFS=$'\t' read -r sha subj pat match; do
        # Include the matched line: "which pattern" alone is rarely enough to
        # see what to delete, especially for a trailer buried in a long body.
        echo "::error::commit ${sha:0:8} (${subj}) matches forbidden pattern '${pat}' at ${match}"
    done < "$REPORT_FILE"
    [ "$SECRET_HITS" -gt 0 ] && echo "::error::$SECRET_HITS added line(s) look like secrets"
    exit 1
fi

echo "✅ commit guard: $SCANNED commit(s) clean"
exit 0
