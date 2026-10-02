#!/usr/bin/env bash
# Install/refresh the Claude login broker on claude-dev. Idempotent; uses sudo. Run after the commit has
# reached /opt/moprox-tooling (sudo systemctl start tooling-pull.service).
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
sudo install -o root -g root -m0600 "$HERE/nftables-claudelogin.conf"  /etc/nftables-claudelogin.conf
sudo install -o root -g root -m0644 "$HERE/claudelogin-gate.service"   /etc/systemd/system/claudelogin-gate.service
sudo install -o root -g root -m0644 "$HERE/claude-login-web.service"   /etc/systemd/system/claude-login-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now claudelogin-gate.service
sudo systemctl reload claudelogin-gate.service
sudo systemctl enable claude-login-web.service >/dev/null
sudo systemctl restart claude-login-web.service
sleep 2
echo "gate:  $(systemctl is-active claudelogin-gate.service)"
echo "web:   $(systemctl is-active claude-login-web.service)"
sudo /usr/sbin/nft list table inet claudelogin | grep -E "accept|drop" | sed 's/^/       /'
curl -sf -o /dev/null -w "local: HTTP %{http_code}\n" http://127.0.0.1:8034/ || echo "local: FAIL"
