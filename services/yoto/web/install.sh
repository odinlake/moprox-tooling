#!/usr/bin/env bash
# Install/refresh yoto-web on claude-dev. Idempotent; uses sudo. Run after the commit has reached
# /opt/moprox-tooling (sudo systemctl start tooling-pull.service).
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
/home/mikael/docindex/venv/bin/python -c 'import paho.mqtt.client, mutagen, PIL' \
  || { echo "ERROR: docindex venv lacks paho-mqtt/mutagen/Pillow" >&2; exit 1; }
mkdir -p /home/mikael/.local/share/moprox/yoto/covers

sudo install -o root -g root -m0600 "$HERE/nftables-yotoweb.conf" /etc/nftables-yotoweb.conf
sudo install -o root -g root -m0644 "$HERE/yotoweb-gate.service"  /etc/systemd/system/yotoweb-gate.service
sudo install -o root -g root -m0644 "$HERE/yoto-web.service"      /etc/systemd/system/yoto-web.service
sudo install -o root -g root -m0644 "$HERE/yoto-sleepguard.service" /etc/systemd/system/yoto-sleepguard.service
sudo install -o root -g root -m0644 "$HERE/yoto-sleepguard.timer"   /etc/systemd/system/yoto-sleepguard.timer
sudo install -o root -g root -m0644 "$HERE/yoto-hafeed.service"     /etc/systemd/system/yoto-hafeed.service
sudo systemctl daemon-reload
sudo systemctl enable --now yoto-sleepguard.timer
sudo systemctl enable yoto-hafeed.service >/dev/null
sudo systemctl restart yoto-hafeed.service
sudo systemctl enable --now yotoweb-gate.service
sudo systemctl reload yotoweb-gate.service
sudo systemctl enable yoto-web.service >/dev/null
sudo systemctl restart yoto-web.service
sleep 2

echo "--- verify ---"
echo "gate:      $(systemctl is-active yotoweb-gate.service)"
echo "yoto-web:  $(systemctl is-active yoto-web.service)"
echo "hafeed:    $(systemctl is-active yoto-hafeed.service)"
echo "sleepguard: $(systemctl is-active yoto-sleepguard.timer), next $(systemctl show -P NextElapseUSecRealtime yoto-sleepguard.timer)"
sudo /usr/sbin/nft list table inet yotoweb | grep -E "accept|drop" | sed 's/^/           /'
curl -sf -o /dev/null -w "local:     HTTP %{http_code}\n" http://127.0.0.1:8030/api/playlists || echo "local:     FAIL"
