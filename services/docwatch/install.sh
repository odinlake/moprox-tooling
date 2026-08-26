#!/usr/bin/env bash
# Install/refresh docwatch on claude-dev. Idempotent; run on claude-dev (uses sudo for the units).
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
sudo install -o root -g root -m0644 "$HERE/docwatch.service" /etc/systemd/system/docwatch.service
sudo install -o root -g root -m0644 "$HERE/docwatch.timer"   /etc/systemd/system/docwatch.timer
sudo systemctl daemon-reload
sudo systemctl enable --now docwatch.timer
echo "--- verify ---"
echo "timer:  $(systemctl is-active docwatch.timer)"
systemctl list-timers docwatch.timer --no-pager | head -2
echo "state:  ${DOCWATCH_STATE:-$HOME/.local/state/docwatch}"
