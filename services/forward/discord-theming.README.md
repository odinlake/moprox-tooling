# discord-theming — talk to the theming agent from a Discord channel

A small bridge: in the configured channel, address the bot and it answers from the **theming** agent
(the totolo MCP + the `theme-ontology/theming` repo). Runs on claude-dev as a `Restart=always`
systemd service, holding an **outbound-only** gateway (WebSocket) connection — no inbound endpoint,
so it suits the isolated/egress-only network.

- **Listens to every message** in the channel, but only **invokes** when the bot is addressed:
  the trigger word `M4` as a real word token (`M4`, `M4?`; not `M40`/`xM4`), a user-mention, or a
  ping of the bot's auto-created **M4 role** (`@M4` resolves to that role). Configurable via
  `DISCORD_TRIGGER`.
- On invocation it **pulls the recent channel history in as context** (Read Message History), so the
  agent sees the conversation, then runs and replies in-channel (chunked to 2000 chars, native
  markdown).
- Both sides are logged to the shared **convo** store, so Discord + Telegram share one timeline.

## One-time Discord setup (your side)
1. **discord.com/developers** → New Application → **Bot** → copy the **Bot Token**.
2. **Bot → Privileged Gateway Intents → enable MESSAGE CONTENT INTENT** → Save. *(Required — without
   it the gateway delivers empty text for non-mention messages, so the `M4` token can't be seen.)*
3. **OAuth2 → URL Generator**: scope `bot`; permissions *View Channel, Send Messages, Read Message
   History*. Open the URL and add the bot to your server. (Adding the bot auto-creates a managed role
   named after it — that's the role `@M4` pings.)
4. Create/choose the channel; Developer Mode on → right-click it → **Copy Channel ID**.
5. Put the creds in `~/.config/claude-dev/discord.env`:
   ```
   DISCORD_BOT_TOKEN=...
   DISCORD_CHANNEL_ID=...
   DISCORD_GUILD_ID=...          # optional
   DISCORD_TRIGGER=M4            # optional (default M4)
   DISCORD_CONTEXT_MSGS=30       # optional: prior messages pulled in as context
   ```

## Deploy (claude-dev)
```
./discord-install.sh     # venv + discord.py + the systemd unit; enables the service if discord.env exists
```
Then in the channel: `M4 what themes cover artificial intelligence?` (or `@M4 …`).

## Notes / gotchas (learned the hard way)
- The bot only receives messages sent **while it's connected** (live gateway stream, not a mailbox) —
  it won't retroactively process a message posted while it was down.
- Editing channel/role permissions briefly drops the bot's access on each save, so API calls in that
  split second can transiently 403 — not a real permission problem.
- Egress hosts (`discord.com`, `gateway.discord.gg`, `*.discord.gg`) are in the Squid allowlist
  (`moprox-homelab/services/squid/README.md`) for when cutover is active.
- Extending to other agents later = another instance with a different channel + AGENT, or a
  channel→agent map.
