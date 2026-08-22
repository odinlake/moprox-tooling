#!/usr/bin/env bash
# Ship the watchdog to the box that hosts the journal sink. Idempotent. Run on claude-dev.
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
H="${H:-agent@logview.lan}"
K="${K:-$HOME/.ssh/claude-dev-ops}"
scp -o BatchMode=yes -o StrictHostKeyChecking=accept-new -i "$K" -q \
    "$HERE/check.py" "$HERE/watch.json" "$HERE/watchdog.service" "$HERE/watchdog.timer" "$H:/tmp/"
ssh -i "$K" "$H" '
  set -e
  sudo install -d -m755 /opt/watchdog
  sudo install -m755 /tmp/check.py   /opt/watchdog/check.py
  sudo install -m644 /tmp/watch.json /opt/watchdog/watch.json
  sudo install -m644 /tmp/watchdog.service /etc/systemd/system/watchdog.service
  sudo install -m644 /tmp/watchdog.timer   /etc/systemd/system/watchdog.timer
  rm -f /tmp/check.py /tmp/watch.json /tmp/watchdog.service /tmp/watchdog.timer
  sudo python3 -m py_compile /opt/watchdog/check.py
  sudo systemctl daemon-reload
  sudo systemctl enable --now watchdog.timer
  echo "watchdog.timer: $(systemctl is-active watchdog.timer)"'
