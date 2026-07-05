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

FWD = "/home/mikael/projects/moprox-tooling/services/forward"

def _theming_out_count():
    return sum(1 for r in convo._load(convo.LOG) if r.get("dir") == "out" and r.get("from") == AGENT)

def _reply_for(text, author, channel_id, message_id, context=""):
    """Run M4. It POSTS its OWN reply via discord_api (owns its output, like coach). Returns
    (posted, reply_text): posted=True if it sent ≥1 message during the run; otherwise the bridge
    relays reply_text as a fallback so nothing is dropped. Blocking."""
    convo.log_in(text, to=AGENT)
    ctx = ("\n\nRecent channel messages (oldest→newest, for context):\n%s" % context) if context else ""
    prompt = ("You are M4 in Discord channel %s, replying to message %s. %s addressed you:\n%r%s\n\n"
              "POST YOUR OWN REPLY — you own your output like a real bot; it is NOT auto-relayed. Use the "
              "discord_api helper via Bash/python:\n"
              "  import sys; sys.path.insert(0, %r); import discord_api\n"
              "  discord_api.post('your **markdown** reply', reply_to=%s)          # text, auto-chunked <2000\n"
              "  discord_api.post_file('/tmp/thing.txt', 'caption', reply_to=%s)   # attach a REAL file\n"
              "To attach a file, WRITE it first (Bash), then post_file it — it uploads from your own "
              "process so it always lands. Post EXACTLY ONCE (a single post, or one post_file whose "
              "caption carries your text). Native Discord markdown; wrap every URL in <angle brackets> "
              "to suppress preview embeds.\n\n"
              "For theme-ontology DATA use the totolo MCP (search / get_document). To add or revise a "
              "theme/story, `git -C ~/projects/theming fetch origin` then branch from origin/master "
              "(`git switch -c ai-feature-<slug> origin/master`) — never master/main, never force — and "
              "say what you changed + that it awaits review (the repo's AGENTS.md is binding). "
              "`convo search <regex>` reaches older cross-platform context.\n\n"
              "Return only a one-line status (e.g. 'posted' / 'attached the table') — do NOT paste the "
              "whole reply back."
              % (channel_id, message_id, author, text, ctx, FWD, message_id, message_id))
    before = _theming_out_count()
    reply = run_agent(AGENT, prompt, timeout=600)
    posted = _theming_out_count() > before          # discord_api.post* logs to convo when it sends
    if not posted:
        convo.log_out(AGENT, reply)                 # fallback relay will post this; log it like before
    return posted, (reply or "(no reply)")

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

# A reply line like `ATTACH: /tmp/foo.txt` or `📎 /tmp/foo.txt` (optional backticks) asks the bridge to
# UPLOAD that file as a real Discord attachment. The marker lines are stripped from the visible text.
ATTACH_RX = re.compile(r"(?im)^[ \t>*-]*(?:ATTACH|📎)[:\s]+`?([^`\n]+?)`?[ \t]*$")
ATTACH_ROOTS = ("/tmp/", str(Path.home()) + "/")
ATTACH_MAX = 8 * 1024 * 1024   # 8 MB — safely under Discord's per-file limit for a non-boosted guild

def _extract_attachments(reply):
    """Pull ATTACH markers out of `reply`. Returns (clean_text, [existing_file_paths])."""
    paths = []
    for m in ATTACH_RX.finditer(reply or ""):
        p = m.group(1).strip()
        if not p.startswith(ATTACH_ROOTS): continue          # only /tmp or the agent's home tree
        try:
            if os.path.isfile(p) and os.path.getsize(p) <= ATTACH_MAX and p not in paths:
                paths.append(p)
        except OSError:
            pass
    clean = ATTACH_RX.sub("", reply or "").strip() if paths else (reply or "")
    return clean, paths

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
    posted = False
    try:
        ctx = await _channel_context(message.channel, message, CONTEXT_N)
        async with message.channel.typing():                        # "typing…" while the agent runs
            posted, reply = await asyncio.to_thread(
                _reply_for, text or "(addressed you with no other text)",
                message.author.display_name, message.channel.id, message.id, ctx)
    except Exception as e:
        posted, reply = False, "⚠️ couldn't finish that — %s" % str(e)[:160]
    if posted:
        return                          # M4 posted its own reply (+ any files) via discord_api — done
    # Fallback (agent returned text but didn't post): relay it — chunked, threaded, legacy ATTACH marker.
    reply, attachments = _extract_attachments(reply)
    chunks = list(_chunks(reply))
    for i, chunk in enumerate(chunks):
        # hang the file(s) off the final text chunk so they land under the reply
        files = [discord.File(p) for p in attachments] if (attachments and i == len(chunks) - 1) else None
        await message.channel.send(chunk, reference=message, mention_author=False, files=files or None)
    if attachments and not chunks:                                   # file-only reply, no text
        await message.channel.send(files=[discord.File(p) for p in attachments],
                                   reference=message, mention_author=False)

def main():
    if not TOKEN: raise SystemExit("missing DISCORD_BOT_TOKEN (set %s)" % ENV)
    client.run(TOKEN)

if __name__ == "__main__":
    main()
