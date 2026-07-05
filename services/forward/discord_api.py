#!/usr/bin/env python3
"""Direct Discord REST for the theming agent (M4) — the analog of tg.py for coach.

M4 composes its reply and posts it ITSELF (text + optional file attachment) to the channel it was
addressed in, instead of returning text for the bridge to relay. This mirrors how coach owns its
Telegram post. Pure stdlib (urllib, hand-rolled multipart — no `discord.py` needed here; that lives
in the gateway bridge). Creds + channel come from ~/.config/claude-dev/discord.env.

  import sys; sys.path.insert(0, "/home/mikael/projects/moprox-tooling/services/forward")
  import discord_api
  discord_api.channel()                                   # the channel id M4 is wired to
  discord_api.post("your **markdown** reply", reply_to=MSG_ID)      # send text (auto-chunked <2000)
  discord_api.post_file("/tmp/table.txt", "caption", reply_to=MSG_ID)  # upload a real attachment

Both post + post_file are logged to the shared convo store (like tg), so Discord + Telegram share one
timeline. Because the file is uploaded from THIS process, there is no cross-process /tmp visibility
problem (the failure mode of the old bridge-side ATTACH-marker relay).
"""
import json, mimetypes, os, urllib.error, urllib.request
from pathlib import Path

import convo   # shared conversation log

ENV = Path(os.environ.get("DISCORD_ENV", Path.home() / ".config/claude-dev/discord.env"))
API = "https://discord.com/api/v10"
AGENT = "theming"
MAXLEN = 2000                              # Discord message content limit
UA = "moprox-m4 (https://themeontology.org, 1.0)"


def _env():
    tok = os.environ.get("DISCORD_BOT_TOKEN"); chan = os.environ.get("DISCORD_CHANNEL_ID")
    if (not tok or not chan) and ENV.exists():
        for ln in ENV.read_text().splitlines():
            ln = ln.strip()
            if ln.startswith("DISCORD_BOT_TOKEN="): tok = ln.split("=", 1)[1].strip()
            if ln.startswith("DISCORD_CHANNEL_ID="): chan = ln.split("=", 1)[1].strip()
    return tok, chan


def channel():
    """The channel id M4 is wired to (from discord.env) — i.e. 'which channel it has been added to'."""
    return _env()[1]


def _chunks(s, n=MAXLEN):
    """Split into Discord-sendable pieces (<2000), preferring a newline boundary."""
    s = (s or "").strip()
    if not s:
        return
    while s:
        cut = s[:n]
        if len(s) > n:
            nl = cut.rfind("\n")
            if nl > n // 2:
                cut = s[:nl]
        yield cut
        s = s[len(cut):].lstrip("\n")


def _post(url, data, headers):
    return json.load(urllib.request.urlopen(
        urllib.request.Request(url, data=data, headers=headers, method="POST"), timeout=30))


def post(text, channel_id=None, reply_to=None):
    """Post a message to the channel (auto-chunked to <2000). reply_to threads it under that message.
    Returns the last created message dict (has 'id'). Mentions are disabled (safe in a shared server)."""
    tok, chan = _env(); chan = str(channel_id or chan or "")
    if not tok or not chan:
        raise SystemExit("discord_api: missing DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID")
    url = "%s/channels/%s/messages" % (API, chan)
    hdr = {"Authorization": "Bot %s" % tok, "Content-Type": "application/json", "User-Agent": UA}
    last, first = None, True
    for chunk in _chunks(text):
        payload = {"content": chunk, "allowed_mentions": {"parse": []}}
        if first and reply_to:
            payload["message_reference"] = {"message_id": str(reply_to), "fail_if_not_exists": False}
        last = _post(url, json.dumps(payload).encode(), hdr); first = False
    if last:
        convo.log_out(AGENT, (text or "").strip(), last.get("id"))
    return last


def post_file(path, text="", channel_id=None, reply_to=None):
    """Upload a file as a REAL Discord attachment, with optional message text. Returns the message dict.
    The file is read + uploaded here in-process, so it always lands (no /tmp cross-process issue)."""
    tok, chan = _env(); chan = str(channel_id or chan or "")
    if not tok or not chan:
        raise SystemExit("discord_api: missing DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID")
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError("discord_api.post_file: no such file %s" % path)
    blob = p.read_bytes()
    payload = {"content": (text or "")[:MAXLEN], "allowed_mentions": {"parse": []},
               "attachments": [{"id": 0, "filename": p.name}]}
    if reply_to:
        payload["message_reference"] = {"message_id": str(reply_to), "fail_if_not_exists": False}
    ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    boundary = "----moprox%d" % (len(blob) & 0xffffff)
    body = ("--%s\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
            "Content-Type: application/json\r\n\r\n%s\r\n" % (boundary, json.dumps(payload))).encode()
    body += ("--%s\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"%s\"\r\n"
             "Content-Type: %s\r\n\r\n" % (boundary, p.name, ctype)).encode()
    body += blob + ("\r\n--%s--\r\n" % boundary).encode()
    hdr = {"Authorization": "Bot %s" % tok, "User-Agent": UA,
           "Content-Type": "multipart/form-data; boundary=%s" % boundary}
    url = "%s/channels/%s/messages" % (API, chan)
    r = _post(url, body, hdr)
    convo.log_out(AGENT, ("📎 %s — %s" % (p.name, text)).strip(" —"), r.get("id"))
    return r


def post_embed(description, title=None, fields=None, color=0x5865F2, channel_id=None, reply_to=None):
    """Post a rich EMBED — a native card that renders markdown and reads well on mobile. Good for a
    bounded, pretty summary. Limits (Discord): description ≤4096, title ≤256, ≤25 fields (name ≤256,
    value ≤1024), ≤6000 total. For content LONGER than that, don't cram an embed — paginate with
    post() instead. `fields` is a list of {name, value, inline?}. Returns the message dict."""
    tok, chan = _env(); chan = str(channel_id or chan or "")
    if not tok or not chan:
        raise SystemExit("discord_api: missing DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID")
    embed = {"description": (description or "")[:4096]}
    if title: embed["title"] = str(title)[:256]
    if color is not None: embed["color"] = int(color)
    if fields:
        embed["fields"] = [{"name": str(f.get("name", "​"))[:256],
                            "value": str(f.get("value", "​"))[:1024],
                            "inline": bool(f.get("inline", False))} for f in fields][:25]
    payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    if reply_to:
        payload["message_reference"] = {"message_id": str(reply_to), "fail_if_not_exists": False}
    hdr = {"Authorization": "Bot %s" % tok, "Content-Type": "application/json", "User-Agent": UA}
    r = _post("%s/channels/%s/messages" % (API, chan), json.dumps(payload).encode(), hdr)
    convo.log_out(AGENT, "[embed] %s" % (title or (description or "")[:80]), r.get("id"))
    return r
