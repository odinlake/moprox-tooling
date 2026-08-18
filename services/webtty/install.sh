#!/usr/bin/env bash
# Install/refresh webtty on claude-dev. Idempotent; run on claude-dev (uses sudo).
#
# ttyd is not packaged for Debian, so this pins an upstream release binary and verifies it against
# upstream's own SHA256SUMS before it is ever made executable.
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VER=1.7.7
SHA=8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55   # ttyd.x86_64, upstream SHA256SUMS

if [[ "$(/usr/local/bin/ttyd --version 2>/dev/null)" == *"$VER"* ]]; then
  echo "==> ttyd $VER already installed"
else
  echo "==> fetching ttyd $VER"
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
  curl -fsSLo "$tmp/ttyd" "https://github.com/tsl0922/ttyd/releases/download/${VER}/ttyd.x86_64"
  echo "${SHA}  ${tmp}/ttyd" | sha256sum -c - >/dev/null || { echo "ERROR: ttyd checksum mismatch" >&2; exit 1; }
  sudo install -o root -g root -m0755 "$tmp/ttyd" /usr/local/bin/ttyd
fi

sudo install -o root -g root -m0600 "$HERE/nftables-webtty.conf" /etc/nftables-webtty.conf
sudo install -o root -g root -m0644 "$HERE/webtty-gate.service"  /etc/systemd/system/webtty-gate.service
sudo install -o root -g root -m0644 "$HERE/webtty.service"       /etc/systemd/system/webtty.service
sudo systemctl daemon-reload
sudo systemctl enable --now webtty-gate.service
sudo systemctl restart webtty.service
sudo systemctl enable webtty.service >/dev/null

echo "--- verify ---"
echo "ttyd:        $(/usr/local/bin/ttyd --version)"
echo "gate:        $(systemctl is-active webtty-gate.service)"
echo "webtty:      $(systemctl is-active webtty.service)"
sudo /usr/sbin/nft list table inet webtty | grep -E "accept|drop" | sed 's/^/             /'
curl -sf -o /dev/null -w "local /tty:    HTTP %{http_code}\n" http://10.10.10.10:7681/tty/ || echo "local /tty:    FAIL"
