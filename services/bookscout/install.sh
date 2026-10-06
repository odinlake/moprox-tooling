#!/usr/bin/env bash
# Install/refresh bookscout on claude-dev. Run after the commit reached /opt/moprox-tooling.
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f /opt/moprox-tooling/services/bookscout/bookscout.py ] || { echo "not deployed to /opt yet" >&2; exit 1; }
install -m0755 "$HERE/bookscout" "$HOME/.local/bin/bookscout"
sudo install -o root -g root -m0644 "$HERE/bookscout.service" /etc/systemd/system/bookscout.service
sudo install -o root -g root -m0644 "$HERE/bookscout.timer"   /etc/systemd/system/bookscout.timer
sudo systemctl daemon-reload
sudo systemctl enable --now bookscout.timer
systemctl list-timers bookscout.timer --no-pager | head -2
