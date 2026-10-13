#!/usr/bin/env bash
# ============================================================================
# patterns.sh — the reference reader of patterns.txt and patterns-strict.txt.
#
# The gate sources it; anything else that wants the same rules (a local
# commit-msg hook, an agent's PreToolUse hook, a PR-title check) runs it, or
# reimplements the three-line format below against a pinned ref. Either way
# there is one list of rules, here.
#
# FORMAT
#   · one POSIX ERE per line, matched case-insensitively, line by line (GNU
#     grep -iE: `\b` is used, `^` anchors at each line of the text);
#   · blank lines and lines starting with `#` are ignored;
#   · `# @scope: <list>` sets the scope of every pattern after it, up to the
#     next directive. <list> is comma separated, from: commit, pr.
#     Before the first directive the scope is `commit`.
#
# usage:
#   patterns.sh list  --scope commit|pr [--strict]       one ERE per line
#   patterns.sh check --scope commit|pr [--strict] < text
#       exit 0 clean · 1 matched (prints "<pattern>\t<first matching line>")
#   both: exit 2 on a usage error, an unreadable file or an empty list.
#
#   PC_PATTERNS_DIR overrides where the files are read from (tests).
# ============================================================================

PC_SCOPES_KNOWN="commit pr"

# pc_load_patterns <file> <array-name> [<scope>]
# Appends to <array-name> every pattern of <file> whose scope includes
# <scope>. Without <scope> every pattern is taken: agent-paths.txt has no
# scopes and is read that way. Returns 2 on an unreadable file or an unknown
# scope, so a typo in a directive fails loudly instead of dropping rules.
pc_load_patterns() {
    local f="$1" want="${3:-}" line cur="commit" s
    local -n dest="$2"
    [ -r "$f" ] || { echo "::error::pattern file not readable: $f" >&2; return 2; }
    while IFS= read -r line || [ -n "$line" ]; do
        if [[ "$line" =~ ^#[[:space:]]*@scope:[[:space:]]*(.*)$ ]]; then
            cur="${BASH_REMATCH[1]//[[:space:]]/}"
            [ -n "$cur" ] || { echo "::error::$f: empty @scope directive" >&2; return 2; }
            local -a parts
            IFS=',' read -r -a parts <<< "$cur"
            for s in "${parts[@]}"; do
                [[ " $PC_SCOPES_KNOWN " == *" $s "* ]] || {
                    echo "::error::$f: unknown scope '$s' (known: $PC_SCOPES_KNOWN)" >&2; return 2; }
            done
            continue
        fi
        case "$line" in ''|'#'*) continue ;; esac
        [ -z "$want" ] || [[ ",$cur," == *",$want,"* ]] || continue
        dest+=("$line")
    done < "$f"
}

pc_patterns_main() {
    local cmd="${1:-}" scope="" strict=false dir
    dir="${PC_PATTERNS_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
    shift || true
    while [ $# -gt 0 ]; do
        case "$1" in
            --scope) scope="${2:-}"; shift 2 || { echo "usage: --scope needs a value" >&2; return 2; } ;;
            --strict) strict=true; shift ;;
            *) echo "usage: unknown argument '$1'" >&2; return 2 ;;
        esac
    done
    case "$cmd" in list|check) ;; *)
        echo "usage: patterns.sh list|check --scope commit|pr [--strict]" >&2; return 2 ;;
    esac
    [[ " $PC_SCOPES_KNOWN " == *" $scope "* ]] && [ -n "$scope" ] || {
        echo "usage: --scope must be one of: $PC_SCOPES_KNOWN" >&2; return 2; }

    local -a pats=()
    pc_load_patterns "$dir/patterns.txt" pats "$scope" || return 2
    if [ "$strict" = true ]; then
        pc_load_patterns "$dir/patterns-strict.txt" pats "$scope" || return 2
    fi
    if [ "${#pats[@]}" -eq 0 ]; then
        echo "::error::no patterns for scope '$scope' — refusing to report a false clean" >&2
        return 2
    fi

    if [ "$cmd" = list ]; then
        printf '%s\n' "${pats[@]}"
        return 0
    fi
    local text p hit rc
    text="$(cat)"
    for p in "${pats[@]}"; do
        hit="$(printf '%s\n' "$text" | grep -iE -m1 -- "$p")"; rc=$?
        # grep's 2 is a broken pattern, not a miss: a rule that cannot match
        # must not be read as a clean verdict.
        [ "$rc" -le 1 ] || { echo "::error::invalid pattern: $p" >&2; return 2; }
        if [ "$rc" -eq 0 ]; then
            printf '%s\t%s\n' "$p" "$(printf '%s' "$hit" | cut -c1-120)"
            return 1
        fi
    done
    return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    set -uo pipefail
    pc_patterns_main "$@"
    exit $?
fi
