#!/usr/bin/env bash
# Install the Discord theming bridge: a venv with discord.py + the systemd service. Idempotent.
# Goes live only once ~/.config/claude-dev/discord.env exists (DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID).
set -Eeuo pipefail
VENV="${VENV:-$HOME/.local/share/moprox/discord-venv}"
ENVF="$HOME/.config/claude-dev/discord.env"
SRC="$(cd "$(dirname "$0")" && pwd)"

python3 -c 'import ensurepip' 2>/dev/null || sudo apt-get install -y python3-venv
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" -q install --upgrade pip
"$VENV/bin/pip" -q install -U 'discord.py>=2.3'
echo "discord.py: $("$VENV/bin/python" -c 'import discord; print(discord.__version__)')"

sudo cp "$SRC/discord-theming.service" /etc/systemd/system/discord-theming.service
sudo systemctl daemon-reload

if [ -f "$ENVF" ]; then
  chmod 600 "$ENVF" 2>/dev/null || true
  sudo systemctl enable --now discord-theming.service
  echo "service: $(systemctl is-active discord-theming.service)"
else
  echo "NOTE: $ENVF not found — create it (0600) with:"
  echo "  DISCORD_BOT_TOKEN=...   DISCORD_CHANNEL_ID=...   [DISCORD_GUILD_ID=...]"
  echo "then: sudo systemctl enable --now discord-theming.service"
fi
