#!/usr/bin/env bash
# Install/refresh the shopping receiver on claude-dev. Idempotent; uses sudo. Run after the commit has
# reached /opt/moprox-tooling (sudo systemctl start tooling-pull.service).
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f /opt/moprox-tooling/services/shopping/shopping.py ] || { echo "not deployed to /opt yet" >&2; exit 1; }
[ -f "$HOME/.config/claude-dev/shopping.env" ] || { echo "missing ~/.config/claude-dev/shopping.env" >&2; exit 1; }
sudo install -o root -g root -m0600 "$HERE/nftables-shopping.conf" /etc/nftables-shopping.conf
sudo install -o root -g root -m0644 "$HERE/shopping-gate.service" /etc/systemd/system/shopping-gate.service
sudo install -o root -g root -m0644 "$HERE/shopping-web.service"  /etc/systemd/system/shopping-web.service
install -m0755 "$HERE/shopping" "$HOME/.local/bin/shopping"
sudo systemctl daemon-reload
sudo systemctl enable --now shopping-gate.service
sudo systemctl reload shopping-gate.service
sudo systemctl enable shopping-web.service >/dev/null
sudo systemctl restart shopping-web.service
sleep 1
echo "gate: $(systemctl is-active shopping-gate.service)  web: $(systemctl is-active shopping-web.service)"
sudo /usr/sbin/nft list table inet shopping | grep -E "accept|drop" | sed 's/^/  /'
