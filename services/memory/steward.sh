#!/usr/bin/env bash
# Memory steward pass — the semantic housekeeping the deterministic reconciler can't do (merge
# conflicts, roll up related facts, demote stale, write DIGEST.md). Runs headless as `claude -p` on
# the Max subscription (never the metered API). Triggered by agent-memory-steward.timer; also fine to
# run by hand. Bounded turns so a misfire can't run away.
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export MEMORY_DIR="${MEMORY_DIR:-$HOME/.claude/projects/-home-mikael/memory}"
cd "$HOME"
# Skip the LLM pass entirely when there's nothing to do (no conflicts and no recent changes).
if [[ ! -s "$MEMORY_DIR/CONFLICTS.md" && -z "$(find "$MEMORY_DIR/journals" -name '*.jsonl' -newermt '-1 day' 2>/dev/null)" ]]; then
  echo "steward: nothing to do (no conflicts, no recent changes)"; exit 0
fi
exec claude -p "$(cat "$HERE/steward-prompt.md")" \
  --permission-mode bypassPermissions \
  --max-turns 40
