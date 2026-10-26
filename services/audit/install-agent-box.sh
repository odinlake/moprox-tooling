#!/bin/bash
# Install the estate audit trail on a box where agents run (claude-dev, claude-loop). Idempotent.
#   sudo services/audit/install-agent-box.sh [TOOLING_ROOT]
# TOOLING_ROOT defaults to this checkout's root: /opt/moprox-tooling on claude-dev (prod tree),
# ~/projects/moprox-tooling on claude-loop (the only tree there; refresh_repos keeps it current).
# See README.md for what each piece answers.
set -euo pipefail
ROOT="${1:-$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)}"
AUDIT="$ROOT/services/audit"
U=mikael; H=/home/$U
[ -x "$AUDIT/agent_action.py" ] || { echo "no audit scripts under $AUDIT" >&2; exit 1; }

# 1. Identity. Author is the AI account for everything on the box; the COMMITTER names the unit, so
#    `git log --format='%an | %cn'` reads "odinlake-ai | odinlake-ai (moprox-dev@one)".
sudo -u $U git config --global user.name odinlake-ai
sudo -u $U git config --global user.email agent@odinlake.net
mkdir -p /etc/systemd/system/service.d
cat > /etc/systemd/system/service.d/50-moprox-agent.conf <<'EOF'
# Every service carries its own unit name as the actor identity (services/audit). Read by the
# Claude hook, git trace2, estate-intent and, via SSH SendEnv, by the box at the other end.
[Service]
Environment=MOPROX_AGENT=%N
Environment="GIT_COMMITTER_NAME=odinlake-ai (%N)"
EOF
systemctl daemon-reload

# 2. git trace2: every git process writes its own record; the shipper turns state changes into
#    journal lines. 1733: anyone may create a file, only root may list or read them.
install -d -m 1733 -o root -g root /var/log/git-trace2
git config --system trace2.eventTarget /var/log/git-trace2
git config --system trace2.eventNesting 1
git config --system trace2.maxFiles 20000
git config --system trace2.envVars MOPROX_AGENT,AGENT_ID,INVOCATION_ID
sed "s#@AUDIT@#$AUDIT#g" "$AUDIT/git-trace-ship.service" > /etc/systemd/system/git-trace-ship.service
install -m 644 "$AUDIT/git-trace-ship.timer" /etc/systemd/system/git-trace-ship.timer
systemctl daemon-reload
systemctl enable --now git-trace-ship.timer

# 3. Claude Code hook: every non-read tool call of every agent on this box, before it runs.
sudo -u $U python3 - "$H/.claude/settings.json" "$AUDIT/agent_action.py" <<'EOF'
import json, sys, os
path, hook = sys.argv[1], sys.argv[2]
s = json.load(open(path)) if os.path.exists(path) else {}
pre = s.setdefault("hooks", {}).setdefault("PreToolUse", [])
pre[:] = [m for m in pre if not any("agent_action.py" in h.get("command", "") for h in m.get("hooks", []))]
pre.append({"matcher": "*", "hooks": [{"type": "command", "command": hook, "timeout": 5}]})
tmp = path + ".tmp"
json.dump(s, open(tmp, "w"), indent=2); os.replace(tmp, path)
EOF

# 4. Tools on PATH: estate-intent for everyone, the gh wrapper ahead of /usr/bin/gh.
ln -sfn "$AUDIT/estate_intent.py" /usr/local/bin/estate-intent
sudo -u $U mkdir -p $H/.local/bin
sudo -u $U ln -sfn "$AUDIT/gh-wrapper.sh" $H/.local/bin/gh

# 5. SSH client: carry the identity to the far end (accepted by ssh-tag.sh on every box).
grep -q "SendEnv MOPROX_AGENT" $H/.ssh/config 2>/dev/null || \
  sudo -u $U sh -c "printf '\n# services/audit: tell the far end which agent this is\nHost *\n    SendEnv MOPROX_AGENT AGENT_ID\n' >> $H/.ssh/config"
echo "audit trail installed from $AUDIT"
