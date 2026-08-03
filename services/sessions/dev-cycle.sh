#!/usr/bin/env bash
# Weekly recycle of ONE moprox-dev@ session (zombie-bridge prevention, Claude Code #57715).
# The session logs its PTY to ~/.local/state/moprox-dev/<id>.log, which `script` TRUNCATES on every
# (re)start — so before restarting we archive the current log dated, and keep ~2 weeks of archives.
# A fresh weekly registration keeps a bridge from living long enough to become a server-side zombie.
#
# The restart itself no longer rotates the registration: moprox-dev-launch resumes, and a plain --resume
# reattaches to the SAME bridgeSessionId (measured — see dev-launch.sh). So we drop a one-shot `.fork`
# flag that makes the launcher fork the thread into a NEW session id, which does rotate the registration.
# Without this flag the weekly recycle would keep the thread but silently stop preventing zombies.
set -euo pipefail
i="${1:?usage: moprox-dev-cycle <one|two|three>}"
L="/home/mikael/.local/state/moprox-dev/$i.log"
F="/home/mikael/.local/state/moprox-dev/$i.fork"
if [ -s "$L" ]; then
  cp -f "$L" "$L.$(date +%Y%m%d)" 2>/dev/null || true
  # keep the 2 most recent weekly archives (~2 weeks); drop older
  ls -1t "$L".[0-9]* 2>/dev/null | tail -n +3 | xargs -r rm -f
fi
# We run as root but the launcher runs as mikael and must be able to delete the flag; chown so the
# one-shot consume can't fail on permissions. (It could delete a root-owned file via the mikael-owned
# directory anyway — this is belt and braces.)
touch "$F" && chown mikael:mikael "$F" 2>/dev/null || true
exec systemctl restart "moprox-dev@$i"
