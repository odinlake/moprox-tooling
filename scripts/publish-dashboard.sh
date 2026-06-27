#!/usr/bin/env bash
# Build the dashboard data + assemble the static site, then publish it to the gh-pages branch
# under /dashboard (served at https://dash.odinlake.net/ and https://odinlake.github.io/moprox-tooling/dashboard/).
#
# Runs on claude-dev (reads the Proxmox token + the private Polar export). Idempotent; safe to
# re-run on a timer. Builds DIRECTLY into the gh-pages checkout and overwrites only the files it
# rebuilds, so a partial publish (e.g. 'dns') leaves the other tabs' data intact.
#
#   scripts/publish-dashboard.sh [system|training|dns|all]   (default: all)
set -Eeuo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WHAT="${1:-all}"
PVE_ENV="${PVE_ENV:-$HOME/.config/proxmox/pve-metrics.env}"
POLAR_RAW="${POLAR_RAW:-$HOME/projects/private-data/polar/raw}"
DNS_EXPORTER="${DNS_EXPORTER:-http://10.10.10.3:9153/}"   # on-subnet HTTP — survives the egress cutover
TMP="$(mktemp -d)"; WT="$(mktemp -d)"
cleanup(){ git -C "$REPO" worktree remove --force "$WT" 2>/dev/null || true; rm -rf "$TMP" "$WT"; }
trap cleanup EXIT

# Check out gh-pages and build straight into it — existing files (other tabs' data, the OAuth
# page, the root index) are preserved; we only overwrite what we rebuild below.
git -C "$REPO" worktree add -q --force "$WT" gh-pages
DASH="$WT/dashboard"; mkdir -p "$DASH/data"
cp "$REPO/components/dashboard/web/index.html" "$DASH/index.html"     # shell is cheap; always refresh
# Per-tab endpoints (dashboard/system/, /dns/, /training/) — same app; it reads the URL to pick
# the tab and fetches from the shared dashboard/data/. So reload/bookmark land on the right tab.
for t in system training; do mkdir -p "$DASH/$t"; cp "$DASH/index.html" "$DASH/$t/index.html"; done

# Custom domain: the site is served at https://dash.odinlake.net (CNAME on Squarespace ->
# odinlake.github.io). Re-write the CNAME + a root redirect EVERY run so a republish/force-push can
# never drop them. The redirect target is relative, so it works both behind the custom domain and at
# odinlake.github.io/moprox-tooling/.
printf 'dash.odinlake.net\n' > "$WT/CNAME"
cat > "$WT/index.html" <<'HTML'
<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>moprox dashboard</title>
<link rel="canonical" href="dashboard/">
<meta http-equiv="refresh" content="0; url=dashboard/">
<script>location.replace("dashboard/"+location.search+location.hash)</script>
</head><body>Redirecting to the <a href="dashboard/">dashboard</a>…</body></html>
HTML

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
  if curl -fsS --max-time 10 "$DNS_EXPORTER" -o "$TMP/dns-raw.json" 2>/dev/null; then
    python3 - "$TMP/dns-raw.json" "$DASH/data/dns/blocked.json" <<'PY'
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

git -C "$WT" add -A
if git -C "$WT" diff --cached --quiet; then
  echo "no changes to publish"
else
  git -C "$WT" commit -q -m "publish dashboard ${WHAT} ($(date -u +%Y-%m-%dT%H:%MZ))" \
    --author="dashboard-bot <noreply@odinlake.net>"
  git -C "$WT" push -q origin gh-pages
  echo "published ($WHAT) -> https://odinlake.github.io/moprox-tooling/dashboard/"
fi
