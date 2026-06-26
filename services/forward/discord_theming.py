#!/usr/bin/env python3
"""Discord bridge for the theming agent: @mention the bot and it answers via the theming agent
(totolo MCP + the theme-ontology repo), logging both sides to the shared convo store so Discord and
Telegram share one conversation timeline.

Design notes:
- Outbound-only gateway (WebSocket) connection — fits the isolated/egress-only network; no inbound
  endpoint to expose.
- **@mention-based**, so it needs NO privileged Message Content intent: Discord delivers the content
  of messages that mention the bot even with default intents. (If content ever arrives empty, flip on
  the Message Content intent in the bot settings.)
- One channel -> one agent, so there's no router; the channel IS the address.
- run_agent() is a blocking subprocess, so it runs in a worker thread to keep the gateway heartbeat
  alive.

Creds from ~/.config/claude-dev/discord.env (DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID[, DISCORD_GUILD_ID]).
"""
import asyncio, os, re, sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / "projects/moprox-tooling/services/agents"))
sys.path.insert(0, str(Path.home() / "projects/moprox-tooling/services/forward"))
from run import run_agent
import convo

import discord

ENV = Path(os.environ.get("DISCORD_ENV", Path.home() / ".config/claude-dev/discord.env"))
AGENT = "theming"

def _load_env():
    if ENV.exists():
        for ln in ENV.read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1); os.environ.setdefault(k, v.strip())

_load_env()
TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID") or 0)   # 0 => answer in any channel it's mentioned in

HISTORY_NOTE = ("You are given only this latest message. If it refers to earlier conversation, run "
                "`convo tail [N]` or `convo search <regex>` to pull just what you need.")

def _reply_for(text, author):
    """Build the prompt, run the theming agent, log both sides to the shared convo store. Blocking."""
    convo.log_in(text, to=AGENT)
    prompt = ("%s sent you this in Discord:\n%r\n\n%s\n\nIf it's a question about the theme ontology's "
              "data, answer it using the totolo MCP (search / get_document). If it asks you to add or "
              "revise a theme/story, prepare the change on an `ai-feature-` BRANCH in ~/projects/theming "
              "(never master/main, never force) and say what you changed + that it awaits review. Reply "
              "concisely for Discord chat (native Discord markdown; no #theming prefix)."
              % (author, text, HISTORY_NOTE))
    reply = run_agent(AGENT, prompt, timeout=600)
    convo.log_out(AGENT, reply)
    return reply or "(no reply)"

def _chunks(s, n=1900):
    """Split a reply into Discord-sendable pieces (<2000 chars), preferring newline boundaries."""
    s = (s or "").strip()
    while s:
        cut = s[:n]
        if len(s) > n:
            nl = cut.rfind("\n")
            if nl > n // 2: cut = s[:nl]
        yield cut
        s = s[len(cut):].lstrip("\n")

intents = discord.Intents.default()    # mentions deliver content without the privileged intent
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print("discord-theming up as %s; channel=%s" % (client.user, CHANNEL_ID or "any"), flush=True)

@client.event
async def on_message(message):
    if message.author.id == client.user.id: return            # ignore our own messages
    if CHANNEL_ID and message.channel.id != CHANNEL_ID: return  # stay in the designated channel
    if client.user not in message.mentions: return            # only act when @mentioned
    text = re.sub(r"<@!?%d>" % client.user.id, "", message.content).strip()
    if not text:
        await message.channel.send("Mention me with a question about the theme ontology.")
        return
    print("theming <-", repr(text[:80]), flush=True)
    try:
        async with message.channel.typing():                  # show "typing…" while the agent runs
            reply = await asyncio.to_thread(_reply_for, text, message.author.display_name)
    except Exception as e:
        reply = "⚠️ couldn't finish that — %s" % str(e)[:160]
    for chunk in _chunks(reply):
        await message.channel.send(chunk, reference=message, mention_author=False)

def main():
    if not TOKEN: raise SystemExit("missing DISCORD_BOT_TOKEN (set %s)" % ENV)
    client.run(TOKEN)

if __name__ == "__main__":
    main()
