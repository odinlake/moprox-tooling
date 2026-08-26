#!/usr/bin/env bash
# Install/refresh docwatch on claude-dev. Idempotent; run on claude-dev (uses sudo for the units).
#
# WHY THE DEPLOY CHECK BELOW EXISTS: this script used to be install-units + `enable --now` + print
# `is-active docwatch.timer`. But docwatch.service does NOT run the tree this script lives in — its
# ExecStart is /opt/moprox-tooling/services/docwatch/docwatch.py, the production checkout that only
# tooling-pull.service writes. So `enable --now` starts a unit whose code this installer never put
# there, and on 2026-08-26T10:09:24Z it did exactly that: the service died with
#   can't open file '/opt/moprox-tooling/services/docwatch/docwatch.py': [Errno 2]
# 46 SECONDS BEFORE b799bb0 — the commit that first added docwatch.py — was even committed. It healed
# itself 68 s later when the next deploy landed, but it spent 8 h at the top of the incident queue at
# score 8, and the installer's own verify block said nothing: `is-active docwatch.timer` reports the
# TIMER, which was perfectly healthy while the service it exists to run was failing. The one signal
# this script printed was green in precisely the case it should have been red.
#
# Hence: prove the payload is deployed BEFORE enabling, and verify the SERVICE, not the timer.
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PROD="${PROD:-/opt/moprox-tooling}"
PAYLOAD="$PROD/services/docwatch/docwatch.py"

sudo install -o root -g root -m0644 "$HERE/docwatch.service" /etc/systemd/system/docwatch.service
sudo install -o root -g root -m0644 "$HERE/docwatch.timer"   /etc/systemd/system/docwatch.timer
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
  echo "REFUSING to enable docwatch.timer: $PAYLOAD still missing after a deploy." >&2
  echo "Enabling now would start a unit with no program to run (see the 2026-08-26 case above)." >&2
  exit 1
fi

sudo systemctl enable --now docwatch.timer

echo "--- verify ---"
echo "timer:   $(systemctl is-active docwatch.timer)"
echo "payload: $PAYLOAD"
# Run the SERVICE once, synchronously. Type=oneshot means `start` blocks and returns non-zero if it
# failed, which is the check the old verify block did not have. is-active is useless for a oneshot —
# a successful run leaves it "inactive" — so report Result= instead.
if sudo systemctl start docwatch.service; then
  echo "service: $(systemctl show -p Result --value docwatch.service) (one run completed)"
else
  echo "service: FAILED — $(systemctl show -p Result --value docwatch.service)" >&2
  systemctl status docwatch.service --no-pager -n 20 || true
  exit 1
fi
systemctl list-timers docwatch.timer --no-pager | head -2
echo "state:   ${DOCWATCH_STATE:-$HOME/.local/state/docwatch}"
