#!/bin/bash
# Make every SSH login on this box name the agent that made it. Idempotent; run on every box.
#   sudo services/audit/ssh-tag.sh          (or pipe it: ssh agent@box 'sudo bash -s' < ssh-tag.sh)
# All agents share one key (claude-dev-ops), so sshd's own "Accepted publickey for agent" line cannot
# tell them apart. The client sends MOPROX_AGENT/AGENT_ID (install-agent-box.sh), sshd accepts it,
# and /etc/ssh/sshrc logs it. Attribution, not authentication: the value is self-declared.
set -euo pipefail
echo "AcceptEnv MOPROX_AGENT AGENT_ID" > /etc/ssh/sshd_config.d/50-moprox-agent.conf
cat > /etc/ssh/sshrc <<'EOF'
# services/audit/ssh-tag.sh: name the agent behind every SSH login. Must stay silent (no stdout).
logger -t agent-ssh "ssh login user=$USER agent=${MOPROX_AGENT:-undeclared}${AGENT_ID:+/$AGENT_ID} from=${SSH_CONNECTION%% *}" </dev/null >/dev/null 2>&1
EOF
chmod 644 /etc/ssh/sshrc
sshd -t 2>/dev/null || /usr/sbin/sshd -t
# Socket-activated sshd (Debian 13: ssh.socket hands its fd to ssh.service) EXITS 255 on SIGHUP, so
# `reload` "fails" on dns/web/monitoring/mcp-google/webscout (2026-10-26) and only works because the
# socket starts a fresh sshd. Restart instead: KillMode=process keeps every open session alive.
if systemctl is-active -q ssh.socket; then systemctl restart ssh.service
elif systemctl is-active -q ssh.service; then systemctl reload ssh.service
elif systemctl is-active -q sshd.service; then systemctl reload sshd.service; fi
echo "$(hostname): ssh agent tagging on"
