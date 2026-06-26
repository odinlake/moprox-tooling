# discord-theming — talk to the theming agent from a Discord channel

A small bridge that lets you **@mention** a bot in a Discord channel and get answers from the
**theming** agent (the totolo MCP + the `theme-ontology/theming` repo). Runs on claude-dev as a
`Restart=always` systemd service, holding an **outbound-only** gateway (WebSocket) connection — no
inbound endpoint, so it suits the isolated/egress-only network.

- **@mention-based**, so it needs **no privileged Message Content intent**.
- One channel → the theming agent (the channel is the address; no router).
- Both sides are logged to the shared **convo** store, so Discord + Telegram share one timeline and
  the agent has continuity (it pulls history on demand with `convo tail` / `convo search`).
- Replies are chunked to Discord's 2000-char limit and rendered as native Discord markdown.

## One-time Discord setup (your side)
1. **discord.com/developers** → New Application → **Bot** → copy the **Bot Token**.
2. **OAuth2 → URL Generator**: scope `bot`; permissions *View Channel, Send Messages, Read Message
   History*. Open the URL and add the bot to your server. (No privileged intents needed.)
3. Create/choose the channel; with Developer Mode on, right-click it → **Copy Channel ID**.
4. Put the creds in `~/.config/claude-dev/discord.env` (0600, gitignored):
   ```
   DISCORD_BOT_TOKEN=...
   DISCORD_CHANNEL_ID=...
   DISCORD_GUILD_ID=...        # optional
   ```

## Deploy (claude-dev)
```
./discord-install.sh     # venv + discord.py + the systemd unit; enables the service if discord.env exists
```
Then `@TheBot what themes cover artificial intelligence?` in the channel.

## Notes
- If message content ever arrives empty (Discord changed something), flip on the **Message Content**
  intent in the bot settings — mentions normally don't require it.
- Egress hosts (`discord.com`, `gateway.discord.gg`, `*.discord.gg`) are registered in the Squid
  allowlist (`moprox-homelab/services/squid/README.md`) for when cutover is active.
- Extending to other agents later = run another instance with a different channel + AGENT, or add a
  channel→agent map.
