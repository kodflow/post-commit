#!/usr/bin/env bash
# ============================================================================
# bump-block-merge.sh — move the stub's pin on kodflow/post-commit/block-merge.
#
# The gate is called at @main on purpose: it holds contents: read and nothing
# else, and a rule fixed here must be live everywhere on the next run. The
# block-merge action is the opposite case. Its job holds `pull-requests: write`,
# and a mutable reference would hand that token to whatever @main becomes. So
# the stub pins it to a full commit SHA, and the pin only ever moves through a
# reviewed change to stub/post-commit.yml — this script writes that change.
#
# Rolling it out needs nothing more: enforce.sh compares every repository with
# the stub, so once the new pin is on main the next enforce run (nightly, or
# `scripts/enforce.sh --apply --all`) opens a sync pull request everywhere.
#
# The SHA must be on main, not on a branch that may be squashed away: a pin to
# a commit main does not contain works only as long as some ref keeps it alive.
#
# usage: bump-block-merge.sh [--check] [<sha>]
#   <sha>    commit to pin; default: the last commit on $REF touching block-merge/
#   --check  change nothing; exit 1 if the pin is not that commit
#   REF      the branch the SHA must be reachable from (default: origin/main)
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
STUB="$ROOT/stub/post-commit.yml"
REF="${REF:-origin/main}"
ACTION="kodflow/post-commit/block-merge"
CHECK=false; WANT=""

while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK=true ;;
        -*) echo "unknown option: $1" >&2; exit 2 ;;
        *) WANT="$1" ;;
    esac
    shift
done

cd "$ROOT"
git rev-parse --verify -q "$REF^{commit}" >/dev/null \
    || { echo "$REF is not a commit here (git fetch origin?)" >&2; exit 2; }
[ -n "$WANT" ] || WANT="$(git log -1 --format=%H "$REF" -- block-merge/)"
WANT="$(git rev-parse --verify -q "$WANT^{commit}")" \
    || { echo "not a commit: $WANT" >&2; exit 2; }
git merge-base --is-ancestor "$WANT" "$REF" \
    || { echo "$WANT is not reachable from $REF: pin a commit main keeps" >&2; exit 1; }
git cat-file -e "$WANT:block-merge/action.yml" 2>/dev/null \
    || { echo "$WANT has no block-merge/action.yml" >&2; exit 1; }

# Exactly one pinned line, or the stub is not what this script understands.
PATTERN="^      - uses: $ACTION@[0-9a-f]{40}( .*)?\$"
n="$(grep -cE "$PATTERN" "$STUB" || true)"
[ "$n" -eq 1 ] || { echo "expected one '$ACTION@<sha>' line in $STUB, found $n" >&2; exit 2; }
OLD="$(grep -E "$PATTERN" "$STUB")"
HAVE="$(printf '%s' "$OLD" | sed -E 's|.*@([0-9a-f]{40}).*|\1|')"

if $CHECK; then
    if [ "$HAVE" = "$WANT" ]; then echo "block-merge pinned to $WANT: current"; exit 0; fi
    echo "block-merge pinned to $HAVE; $REF has $WANT"
    exit 1
fi

DATE="$(git log -1 --format=%cs "$WANT")"
NEW="      - uses: $ACTION@$WANT  # $DATE"
# A literal comparison, not a regex: not every awk knows `{40}`.
OLD="$OLD" NEW="$NEW" awk '$0 == ENVIRON["OLD"] { print ENVIRON["NEW"]; next } { print }' "$STUB" > "$STUB.tmp"
mv "$STUB.tmp" "$STUB"
echo "block-merge: $HAVE -> $WANT ($DATE)"
