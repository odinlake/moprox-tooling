#!/usr/bin/env bash
# Keep the PRODUCTION checkout of moprox-tooling at origin/main.
#
# WHY THIS EXISTS: for months the 18 systemd units executed code straight out of
# ~/projects/moprox-tooling — the same working tree three interactive sessions and five agents
# develop in. Checking out a feature branch there silently changed what production ran, and a commit
# made while the tree sat on someone else's branch went nowhere useful. Deployment and development
# cannot share a working tree; webscout already solved this with /opt/webscout/repo + deploy.sh.
#
# This clone is NOT a workspace: it is reset --hard to origin/main every run. Never edit it, never
# commit in it — anything local is discarded without warning, which is the point.
set -Eeuo pipefail
PROD="${PROD:-/opt/moprox-tooling}"
BRANCH="${BRANCH:-main}"
LOG="${LOG:-/home/mikael/.local/state/tooling-pull.log}"
mkdir -p "$(dirname "$LOG")"; exec >>"$LOG" 2>&1
say() { echo "$(date -Is) $*"; }

cd "$PROD" || { say "FATAL: $PROD missing"; exit 1; }
before=$(git rev-parse --short HEAD)
git fetch -q origin "$BRANCH" || { say "WARN: fetch failed"; exit 1; }
after=$(git rev-parse --short "origin/$BRANCH")
[ "$before" = "$after" ] && exit 0

# Local state in a deployment tree is always a mistake; report it, then discard.
if [ -n "$(git status --porcelain)" ]; then
  say "WARN: local changes in the PRODUCTION tree — discarding: $(git status --porcelain | head -3 | tr '\n' ' ')"
fi
git reset -q --hard "origin/$BRANCH"
git clean -qfd
say "deployed $before -> $after ($(git log -1 --format=%s | cut -c1-70))"

# A unit whose code changed keeps running the old module until it restarts (the stale-daemon trap
# that once cost a false 'valet memory is blocked' report). Restart the long-lived importers only.
if git diff --name-only "$before" "$after" | grep -qE '^services/(agents|forward)/'; then
  systemctl restart dispatcher.service telegram-poll.service discord-theming.service 2>/dev/null \
    && say "restarted agent daemons (services/agents or services/forward changed)"
fi
