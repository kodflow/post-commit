#!/usr/bin/env bash
# ============================================================================
# bump-runner-template.sh — move every runner-template stub to one commit of
# kodflow/runner-template.
#
# The stubs under stub/runner-template/<owner>/ call kodflow/runner-template's
# reusable workflows at a full commit SHA, twice per stub (`uses:` takes no
# expression, so the same SHA is passed as `ref`). A change there reaches the
# owners only when this pin moves, through a reviewed change here — this script
# writes that change, in every stub at once, so the fleet never runs two
# versions of the logic.
#
# Rolling it out needs nothing more: enforce.sh compares every
# <owner>/runner-template with these stubs, so once the new pin is on main the
# next enforce run (nightly, or `scripts/enforce.sh --apply --all`) opens a
# sync pull request on each of them.
#
# The SHA must be on kodflow/runner-template's main, not on a branch that may
# be squashed away, and every reusable workflow a stub calls must exist at it.
#
# usage: bump-runner-template.sh [--check | --verify] [<sha>]
#   <sha>     commit to pin; default: the tip of kodflow/runner-template main
#   --check   change nothing; exit 1 if the stubs are not pinned to <sha>
#   --verify  change nothing; exit 1 unless the current pin is on main and
#             carries every workflow the stubs call (what CI runs)
#   RT_DIR    a clone of kodflow/runner-template to read (default: a
#             throwaway anonymous clone); RT_REF the branch the SHA must be
#             reachable from in it (default: origin/main)
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
STUBS="$ROOT/stub/runner-template"
RT_REF="${RT_REF:-origin/main}"
MODE=bump; WANT=""

while [ $# -gt 0 ]; do
    case "$1" in
        --check) MODE=check ;;
        --verify) MODE=verify ;;
        -*) echo "unknown option: $1" >&2; exit 2 ;;
        *) WANT="$1" ;;
    esac
    shift
done

if [ -z "${RT_DIR:-}" ]; then
    RT_DIR="$(mktemp -d)"
    trap 'rm -rf "$RT_DIR"' EXIT
    git clone -q --filter=blob:none --no-checkout https://github.com/kodflow/runner-template.git "$RT_DIR" \
        || { echo "cannot clone kodflow/runner-template" >&2; exit 2; }
fi
git -C "$RT_DIR" rev-parse --verify -q "$RT_REF^{commit}" >/dev/null \
    || { echo "$RT_REF is not a commit in $RT_DIR (git fetch origin?)" >&2; exit 2; }

# The one pin every stub carries now, or the stubs are not what this script
# understands.
HAVE="$(grep -hoE '(@|^      ref: )[0-9a-f]{40}$' "$STUBS"/*/*.yml | grep -oE '[0-9a-f]{40}' | sort -u)"
[ "$(printf '%s\n' "$HAVE" | grep -c .)" -eq 1 ] \
    || { echo "the stubs carry $(printf '%s\n' "$HAVE" | grep -c .) different pins; expected one" >&2; exit 2; }

if [ "$MODE" = verify ]; then WANT="$HAVE"; fi
[ -n "$WANT" ] || WANT="$(git -C "$RT_DIR" rev-parse "$RT_REF")"
WANT="$(git -C "$RT_DIR" rev-parse --verify -q "$WANT^{commit}")" \
    || { echo "not a commit of kodflow/runner-template: $WANT" >&2; exit 2; }
git -C "$RT_DIR" merge-base --is-ancestor "$WANT" "$RT_REF" \
    || { echo "$WANT is not reachable from $RT_REF: pin a commit main keeps" >&2; exit 1; }
missing=0
while IFS= read -r wf; do
    git -C "$RT_DIR" cat-file -e "$WANT:.github/workflows/$wf" 2>/dev/null \
        || { echo "$WANT has no .github/workflows/$wf, which a stub calls" >&2; missing=1; }
done < <(grep -hoE 'kodflow/runner-template/\.github/workflows/reusable-[a-z0-9-]+\.yml' "$STUBS"/*/*.yml \
            | sed 's|.*/||' | sort -u)
[ "$missing" -eq 0 ] || exit 1

case "$MODE" in
    verify) echo "runner-template pinned to $WANT: on $RT_REF, every called workflow present"; exit 0 ;;
    check)
        if [ "$HAVE" = "$WANT" ]; then echo "runner-template pinned to $WANT: current"; exit 0; fi
        echo "runner-template pinned to $HAVE; $RT_REF has $WANT"
        exit 1 ;;
esac

for f in "$STUBS"/*/*.yml; do
    sed -E "s/(@|^      ref: )$HAVE\$/\\1$WANT/" "$f" > "$f.tmp" && mv "$f.tmp" "$f"
done
echo "runner-template: $HAVE -> $WANT"
