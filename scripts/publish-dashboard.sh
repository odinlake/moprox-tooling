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
DNS_EXPORTER="${DNS_EXPORTER:-http://10.10.10.3:9153/}"   # on-subnet HTTP — survives the egress cutover
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
if [[ "$WHAT" == all || "$WHAT" == dns ]]; then
  mkdir -p "$DASH/data/dns"
  if curl -fsS --max-time 10 "$DNS_EXPORTER" -o "$STAGE/dns-raw.json" 2>/dev/null; then
    python3 - "$STAGE/dns-raw.json" "$DASH/data/dns/blocked.json" <<'PY'
import sys, json, time
d = json.load(open(sys.argv[1]))
# publish only blocked-by-day + totals — NOT the per-client IPs the exporter also exposes
out = {"generated": int(time.time()), "totals": d.get("totals", {}), "blocked_days": d.get("blocked_days", [])}
json.dump(out, open(sys.argv[2], "w"), separators=(",", ":"))
PY
    echo "wrote $DASH/data/dns/blocked.json"
  else
    echo "WARN: DNS exporter unreachable ($DNS_EXPORTER) — skipping dns data"
  fi
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
