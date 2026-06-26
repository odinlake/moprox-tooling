#!/usr/bin/env python3
"""Discord bridge for the theming agent.

It listens to EVERY message in the configured channel, but only INVOKES the theming agent when the
bot's trigger name appears as a real word token — `M4`, `@M4`, `M4?` fire; `M40` / `xM4` don't
(word-boundary, punctuation-aware, case-insensitive). On invocation it pulls the recent channel
history (Read Message History permission) in as context, runs the theming agent (totolo MCP + the
theme-ontology/theming repo), and posts the reply. Both sides are logged to the shared convo store so
Discord + Telegram share one timeline.

Requires the **Message Content** privileged intent: requested here in code AND enabled in the
Developer Portal (Bot → Privileged Gateway Intents → Message Content). Without it the gateway delivers
empty text for non-mention messages, so the trigger can't be seen.

Connection is an outbound-only gateway (WebSocket) — fits the isolated/egress-only network. run_agent
is a blocking subprocess, so it runs in a worker thread to keep the gateway heartbeat alive.

Creds/config from ~/.config/claude-dev/discord.env:
  DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID[, DISCORD_GUILD_ID]
  DISCORD_TRIGGER       trigger word (default "M4")
  DISCORD_CONTEXT_MSGS  how many prior channel messages to pull as context (default 30)
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
CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID") or 0)   # 0 => listen in every channel it can see
TRIGGER = os.environ.get("DISCORD_TRIGGER", "M4")
CONTEXT_N = int(os.environ.get("DISCORD_CONTEXT_MSGS", "30"))
# the trigger word as a standalone token: optional leading @, not flanked by word chars (so "M4",
# "@M4", "M4!", "(M4)" match; "M40", "xM4", "HM4" do not). Case-insensitive.
TRIG_RX = re.compile(r"(?i)(?<!\w)@?" + re.escape(TRIGGER) + r"(?!\w)")

def _reply_for(text, author, context=""):
    """Build the prompt (with channel context), run the theming agent, log both sides. Blocking."""
    convo.log_in(text, to=AGENT)
    ctx = ("\n\nRecent channel messages (oldest→newest, for context):\n%s" % context) if context else ""
    prompt = ("You are M4 in a Discord channel. %s addressed you:\n%r%s\n\n"
              "Answer their latest message. For theme-ontology data use the totolo MCP "
              "(search / get_document). To add or revise a theme/story, prepare the change on an "
              "`ai-feature-` BRANCH in ~/projects/theming (never master/main, never force) and say "
              "what you changed + that it awaits review. You can also `convo search <regex>` for older "
              "cross-platform context. Reply concisely in native Discord markdown (no #theming prefix)."
              % (author, text, ctx))
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

async def _channel_context(channel, before_msg, n):
    """Pull the last n messages before `before_msg` (REST history; needs Read Message History), as
    'Author: text' lines, oldest→newest. REST returns content regardless of the gateway intent."""
    rows = []
    async for m in channel.history(limit=n, before=before_msg):
        c = (m.content or "").strip()
        if c: rows.append("%s: %s" % (m.author.display_name, c))
    rows.reverse()
    return "\n".join(rows)

intents = discord.Intents.default()
intents.message_content = True         # REQUIRED to read channel text (also enable it in the Dev Portal)
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print("discord-theming up as %s; channel=%s; trigger=%r" % (client.user, CHANNEL_ID or "any", TRIGGER), flush=True)

@client.event
async def on_message(message):
    if message.author.id == client.user.id: return                  # ignore our own messages
    if CHANNEL_ID and message.channel.id != CHANNEL_ID: return      # only the configured channel
    content = message.content or ""
    bot_role_ids = {r.id for r in message.guild.me.roles} if message.guild else set()
    mentioned = client.user in message.mentions                     # @M4 the user
    role_pinged = any(r.id in bot_role_ids for r in message.role_mentions)  # @M4 the bot's managed role
    triggered = mentioned or role_pinged or bool(TRIG_RX.search(content))   # or the literal "M4" token
    print("rx: ch=%s mention=%s role=%s trig=%s len=%d from=%s" % (
        message.channel.id, mentioned, role_pinged, triggered, len(content), message.author.display_name), flush=True)
    if not triggered: return                                        # listen to all, invoke only when addressed
    # the question: strip the bot's user/role mention markup + the trigger token
    text = content
    for _id in ({client.user.id} | bot_role_ids):
        text = text.replace("<@%d>" % _id, "").replace("<@!%d>" % _id, "").replace("<@&%d>" % _id, "")
    text = TRIG_RX.sub("", text).strip()
    print("theming <-", repr(text[:80]), flush=True)
    try:
        ctx = await _channel_context(message.channel, message, CONTEXT_N)
        async with message.channel.typing():                        # "typing…" while the agent runs
            reply = await asyncio.to_thread(_reply_for, text or "(addressed you with no other text)",
                                            message.author.display_name, ctx)
    except Exception as e:
        reply = "⚠️ couldn't finish that — %s" % str(e)[:160]
    for chunk in _chunks(reply):
        await message.channel.send(chunk, reference=message, mention_author=False)

def main():
    if not TOKEN: raise SystemExit("missing DISCORD_BOT_TOKEN (set %s)" % ENV)
    client.run(TOKEN)

if __name__ == "__main__":
    main()
