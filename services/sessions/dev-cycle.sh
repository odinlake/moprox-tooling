#!/usr/bin/env bash
# Weekly recycle of ONE moprox-dev@ session (zombie-bridge prevention, Claude Code #57715).
# The session logs its PTY to ~/.local/state/moprox-dev/<id>.log, which `script` TRUNCATES on every
# (re)start — so before restarting we archive the current log dated, and keep ~2 weeks of archives.
# A fresh weekly registration keeps a bridge from living long enough to become a server-side zombie.
set -euo pipefail
i="${1:?usage: moprox-dev-cycle <one|two|three>}"
L="/home/mikael/.local/state/moprox-dev/$i.log"
if [ -s "$L" ]; then
  cp -f "$L" "$L.$(date +%Y%m%d)" 2>/dev/null || true
  # keep the 2 most recent weekly archives (~2 weeks); drop older
  ls -1t "$L".[0-9]* 2>/dev/null | tail -n +3 | xargs -r rm -f
fi
exec systemctl restart "moprox-dev@$i"
