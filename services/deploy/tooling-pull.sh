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
#
# AND NEVER RUN git HERE AS ROOT. `sudo git pull` in this tree leaves the object fanout dirs it
# happens to create owned by root; this unit runs as mikael, so the NEXT fetch that needs one of
# those dirs — out of 256, so typically hours or DAYS later — dies with "insufficient permission for
# adding an object". Measured twice, both by moprox-dev@one: 2026-08-08T19:32 -> 13 failures from
# 19:55, and 2026-08-11T10:33 -> 58 failures from 08-14T05:15, i.e. 67 h after the cause. The delay
# is what makes it recur: whoever types it gets no feedback, and the session that fixed it the first
# time repeated it three days later.
#
# To deploy right now, do not reach for git at all:  sudo systemctl start tooling-pull.service
# (the unit runs as mikael and is the only supported writer of this tree).
set -Eeuo pipefail
PROD="${PROD:-/opt/moprox-tooling}"
BRANCH="${BRANCH:-main}"
LOG="${LOG:-/home/mikael/.local/state/tooling-pull.log}"
mkdir -p "$(dirname "$LOG")"
# Tee rather than redirect. `exec >>"$LOG" 2>&1` sent EVERYTHING to a file, so a failing unit showed
# up in journald and the incident queue with no reason at all — 13 consecutive failures whose cause
# ("insufficient permission for adding an object", from a root-owned .git) was only in a file nobody
# was watching. Repo rule: an unexpected error reaches the journal.
exec > >(tee -a "$LOG") 2> >(tee -a "$LOG" >&2)
say() { echo "$(date -Is) $*"; }
# <3> is the syslog level prefix journald turns into PRIORITY=3, so failures are findable with
# search_logs(priority=3) across the fleet.
die() { echo "<3>$(date -Is) $*" >&2; exit 1; }

cd "$PROD" || die "FATAL: $PROD missing"
before=$(git rev-parse --short HEAD)
# Report what git ACTUALLY said, at err level. This line used to name one guessed cause on every
# failure ("a root-owned object blocks the mikael-run unit"). On 2026-08-13T14:35Z the real cause was
# `git@github.com: Permission denied (publickey)` — present in the journal, but only at priority 6,
# below the level the comment above sends triage to. The single priority-3 line said "check
# ownership". A hard-coded diagnosis on a generic failure is worse than no diagnosis: it is confident
# and wrong. Capture stderr so the cause and the alert are the same line.
if ! fetch_err=$(git fetch -q origin "$BRANCH" 2>&1); then
  die "fetch failed: $(printf '%s' "$fetch_err" | tr '\n' ' ' | cut -c1-300)"
fi
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
  # sudo -n: the unit runs as mikael; /etc/sudoers.d/tooling-pull grants exactly this one command.
  sudo -n systemctl restart dispatcher.service telegram-poll.service discord-theming.service 2>/dev/null \
    && say "restarted agent daemons (services/agents or services/forward changed)"
fi
