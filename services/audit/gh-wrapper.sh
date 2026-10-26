#!/bin/bash
# gh wrapper (installed as ~/.local/bin/gh, ahead of /usr/bin/gh on every agent's PATH).
#
# Only `gh pr merge` is touched; everything else execs the real gh untouched. For a merge it
#   1. prints any estate-intent still open on that repo (a running loop cycle, another session's
#      deploy) -- advisory, never blocks;
#   2. writes "Merged-by: odinlake-ai (<agent>)" and any concurrent intents into the squash commit,
#      because GitHub records every agent's merge as the same bot account.
# Why: 2026-10-26, two PRs merged under a running loop cycle and nobody could tell who had done it.
REAL=/usr/bin/gh
[ "${1:-}" = pr ] && [ "${2:-}" = merge ] || exec "$REAL" "$@"

AUDIT="$(dirname "$(readlink -f "$0")")"
AGENT="$(python3 -c "import sys; sys.path.insert(0, '$AUDIT'); import who; print(who.agent())" 2>/dev/null || echo unknown)"
REPO="$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null)"
REPO_ARG=()
for ((i = 1; i <= $#; i++)); do
  if [ "${!i}" = -R ] || [ "${!i}" = --repo ]; then j=$((i + 1)); REPO="$(basename "${!j}")"; REPO_ARG=(-R "${!j}"); fi
done

CONCURRENT=""
if [ -n "$REPO" ]; then
  CONCURRENT="$("$AUDIT/estate_intent.py" check "repo:$REPO" 2>/dev/null)"
  [ -n "$CONCURRENT" ] && printf 'gh pr merge: other work is open on %s:\n%s\n' "$REPO" "$CONCURRENT" >&2
fi

args=("${@:3}")
has_body=0; squash=0; num=""
for a in "${args[@]}"; do
  case "$a" in -b|--body|--body-file|-F) has_body=1 ;; -s|--squash) squash=1 ;; [0-9]*) num="$a" ;; esac
done
if [ "$squash" = 1 ] && [ "$has_body" = 0 ]; then
  # GitHub's own squash default is the commits' messages; rebuild that, then add the trailer.
  BODY="$("$REAL" pr view $num "${REPO_ARG[@]}" --json commits -q '.commits | if length == 1 then .[0].messageBody else map("* " + .messageHeadline + (if .messageBody != "" then "\n\n" + .messageBody else "" end)) | join("\n\n") end' 2>/dev/null)"
  BODY="$BODY"$'\n\n'"Merged-by: odinlake-ai ($AGENT)"
  [ -n "$CONCURRENT" ] && BODY="$BODY"$'\n'"Concurrent-intents: $(printf %s "$CONCURRENT" | tr '\n' ';')"
  args+=(--body "$BODY")
fi
logger -t estate-intent "INTENT note id=- scope=repo:${REPO:-?} by=$AGENT :: gh pr merge ${*:3}"
exec "$REAL" pr merge "${args[@]}"
