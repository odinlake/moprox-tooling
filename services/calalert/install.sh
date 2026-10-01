#!/usr/bin/env bash
# Install/refresh calalert on claude-dev. Idempotent; run on claude-dev (uses sudo for the units).
#
# The checks below are copied from docwatch/install.sh, which explains them at length: the unit runs
# /opt/moprox-tooling (only tooling-pull writes there), so prove the payload is deployed BEFORE
# enabling, and verify the SERVICE with a real run, not the timer.
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PROD="${PROD:-/opt/moprox-tooling}"
PAYLOAD="$PROD/services/calalert/calalert.py"

sudo install -o root -g root -m0644 "$HERE/calalert.service" /etc/systemd/system/calalert.service
sudo install -o root -g root -m0644 "$HERE/calalert.timer"   /etc/systemd/system/calalert.timer
sudo systemctl daemon-reload

# The payload reaches $PROD only via origin/main. If this tree's HEAD is not pushed, no deploy can
# ever produce it and the wait below would be a slow way to fail — so say the real reason now.
if git -C "$HERE" rev-parse --git-dir >/dev/null 2>&1; then
  head=$(git -C "$HERE" rev-parse HEAD)
  if ! git -C "$HERE" merge-base --is-ancestor "$head" origin/main 2>/dev/null; then
    echo "REFUSING: $HERE HEAD ${head:0:7} is not an ancestor of origin/main, so tooling-pull can" >&2
    echo "never deploy it to $PROD. Push first, then re-run this script." >&2
    exit 1
  fi
fi

# Deploy, then confirm. tooling-pull is a oneshot and blocks; it exits 0 when already up to date.
if [ ! -f "$PAYLOAD" ]; then
  echo "payload absent from $PROD — deploying first"
  sudo systemctl start tooling-pull.service || true
fi
if [ ! -f "$PAYLOAD" ]; then
  echo "REFUSING to enable calalert.timer: $PAYLOAD still missing after a deploy." >&2
  echo "Enabling now would start a unit with no program to run (see the 2026-08-26 case above)." >&2
  exit 1
fi

sudo systemctl enable --now calalert.timer

echo "--- verify ---"
echo "timer:   $(systemctl is-active calalert.timer)"
echo "payload: $PAYLOAD"
# Run the SERVICE once, synchronously. Type=oneshot means `start` blocks and returns non-zero if it
# failed, which is the check the old verify block did not have. is-active is useless for a oneshot —
# a successful run leaves it "inactive" — so report Result= instead.
if sudo systemctl start calalert.service; then
  echo "service: $(systemctl show -p Result --value calalert.service) (one run completed)"
else
  echo "service: FAILED — $(systemctl show -p Result --value calalert.service)" >&2
  systemctl status calalert.service --no-pager -n 20 || true
  exit 1
fi
systemctl list-timers calalert.timer --no-pager | head -2
echo "state:   ${CALALERT_STATE:-$HOME/.local/state/calalert/state.json}"
