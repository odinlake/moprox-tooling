#!/usr/bin/env bash
# Build the dashboard data + assemble the static site, then publish it to the gh-pages branch
# under /dashboard (served at https://odinlake.github.io/moprox-tooling/dashboard/).
#
# Runs on claude-dev (reads the Proxmox token + the private Polar export). Idempotent; safe to
# re-run on a timer. Keeps gh-pages history flat by amending a single "publish" commit.
#
#   scripts/publish-dashboard.sh [system|training|all]   (default: all)
set -Eeuo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WHAT="${1:-all}"
STAGE="$(mktemp -d)"; DASH="$STAGE/dashboard"
PVE_ENV="${PVE_ENV:-$HOME/.config/proxmox/pve-metrics.env}"
POLAR_RAW="${POLAR_RAW:-$HOME/projects/private-data/polar/raw}"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$DASH/data"
cp "$REPO/components/dashboard/web/index.html" "$DASH/index.html"

if [[ "$WHAT" == all || "$WHAT" == system ]]; then
  [[ -f "$PVE_ENV" ]] || { echo "no $PVE_ENV"; exit 1; }
  set -a; . "$PVE_ENV"; set +a
  OUT="$DASH/data" python3 "$REPO/services/metrics/rrd_json.py" all
fi
if [[ "$WHAT" == all || "$WHAT" == training ]]; then
  POLAR_RAW="$POLAR_RAW" OUT="$DASH/data/training/sessions.json" python3 "$REPO/services/training/build.py"
fi

# --- publish to gh-pages/dashboard via a worktree, flat history ---
WT="$(mktemp -d)"
git -C "$REPO" worktree add -q --force "$WT" gh-pages
rm -rf "$WT/dashboard"; mkdir -p "$WT/dashboard"
cp -r "$DASH/." "$WT/dashboard/"
git -C "$WT" add -A
if git -C "$WT" diff --cached --quiet; then
  echo "no changes to publish"
else
  git -C "$WT" commit -q -m "publish dashboard ($(date -u +%Y-%m-%dT%H:%MZ))" \
    --author="dashboard-bot <noreply@odinlake.net>"
  git -C "$WT" push -q origin gh-pages
  echo "published -> https://odinlake.github.io/moprox-tooling/dashboard/"
fi
git -C "$REPO" worktree remove --force "$WT"
