#!/usr/bin/env bash
# STAND-IN deploy: run the totolo-search MCP server ON claude-dev and expose it AT THE TOTOLO
# ENDPOINT (totolo.lan), so the theming agent (and anything else) can use it exactly as it will
# use the real LXC later. When moprox-homelab/services/totolo is provisioned, run ./uninstall.sh
# here and the `totolo.lan` name resolves to the real container instead — no consumer changes.
#
# Mirrors the container's setup (CPU-only torch, requirements, package, prebuilt index, the same
# serve_http.py launcher / streamable-http on 0.0.0.0:8765). Idempotent. Run as the mikael user.
set -Eeuo pipefail
APP="${APP:-/opt/totolo-search}"
USER_="${USER_:-mikael}"
PORT="${MCP_PORT:-8765}"
REPO_URL="${REPO_URL:-https://github.com/theme-ontology/python-totolo-search}"
REF="${REF:-main}"
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "==> system deps (git, python venv)"
command -v git >/dev/null || sudo apt-get install -y git
python3 -c 'import ensurepip' 2>/dev/null || sudo apt-get install -y python3-venv

echo "==> app dir ${APP} (owned by ${USER_})"
sudo install -d -o "$USER_" -g "$USER_" "$APP"

echo "==> checkout + venv + deps (CPU torch, requirements, package)"
[ -d "$APP/src" ] || git clone --depth 1 -b "$REF" "$REPO_URL" "$APP/src"
[ -d "$APP/venv" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" -q install --upgrade pip wheel
"$APP/venv/bin/pip" -q install --index-url https://download.pytorch.org/whl/cpu torch
"$APP/venv/bin/pip" -q install -r "$APP/src/requirements.txt"
"$APP/venv/bin/pip" -q install "$APP/src"
cp "$SRC/serve_http.py" "$APP/serve_http.py"

echo "==> build the search index (first run downloads the ontology + embedding model)"
[ -f "$APP/.totolo_search/embeddings.npy" ] || HOME="$APP" "$APP/venv/bin/totolo-search" --build-index

echo "==> systemd service (bind 0.0.0.0:${PORT})"
printf 'MCP_HOST=0.0.0.0\nMCP_PORT=%s\nHOME=%s\n' "$PORT" "$APP" | sudo tee /etc/totolo-standin.env >/dev/null
sudo cp "$SRC/totolo-mcp.service" /etc/systemd/system/totolo-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now totolo-mcp.service

echo "==> expose at the totolo endpoint (name resolves to THIS VM until the LXC exists)"
if ! grep -q 'totolo.lan' /etc/hosts; then
  echo '10.10.10.10 totolo.lan totolo  # STAND-IN (moprox-tooling/services/totolo-standin) — remove when the totolo LXC is up' | sudo tee -a /etc/hosts >/dev/null
fi

echo "==> verify"
for i in $(seq 1 60); do curl -s -o /dev/null -m2 "http://totolo.lan:${PORT}/mcp" && break; sleep 1; done
code=$(curl -s -o /dev/null -m5 -w '%{http_code}' "http://totolo.lan:${PORT}/mcp" || true)
echo "    http://totolo.lan:${PORT}/mcp -> HTTP ${code} (406 on a bare GET == server up)"
echo "DONE."
