#!/usr/bin/env bash
# Undo the stand-in once the real totolo LXC (moprox-homelab/services/totolo) is up: stop the local
# server and drop the /etc/hosts override so `totolo.lan` resolves (via the DNS box) to the real
# container instead. Leaves /opt/totolo-search in place (delete by hand if you want the disk back).
set -Eeuo pipefail
sudo systemctl disable --now totolo-mcp.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/totolo-mcp.service /etc/totolo-standin.env
sudo systemctl daemon-reload
sudo sed -i '/STAND-IN (moprox-tooling\/services\/totolo-standin)/d' /etc/hosts
echo "stand-in removed. 'totolo.lan' now resolves via DNS (the real LXC). 'rm -rf /opt/totolo-search' to reclaim disk."
