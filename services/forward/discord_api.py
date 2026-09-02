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
import json, re, mimetypes, os, urllib.error, urllib.request
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


# A bare URL makes Discord render a preview card, which for a PR link is several lines of
# repository chrome under a one-line message. `<angle brackets>` suppress it. Every agent context in
# the estate already says to do this and it still gets forgotten, so the transport does it — the
# same reason tg.py owns the #handle rather than trusting each caller to remember.
# No trailing lookahead here on purpose: one used to be, to avoid double-wrapping, and it made the
# match BACKTRACK — "(see https://example.com/z)" wrapped as "<https://example.com/>z" because the
# shorter match was the one whose next character satisfied the lookahead. The leading lookbehind
# already covers everything it was there for.
_BARE_URL = re.compile(r"""
    (?<![<(\[`])                 # not already wrapped, and not a markdown link's ( or [
    (https?://[^\s<>()\[\]`]+)   # the url, stopping at whitespace or a bracket of any kind
""", re.VERBOSE)
_TRAILING = ".,;:!?'\""


def _wrap(m):
    """Wrap one url, leaving sentence punctuation that merely follows it outside the brackets."""
    u = m.group(1).rstrip(_TRAILING)
    return "<%s>%s" % (u, m.group(1)[len(u):])


def link(text, url):
    """A masked link that renders no preview: [text](<url>).

    Discord shows only `text`, and the angle brackets INSIDE the target are what suppress the embed
    — a plain [text](url) can still expand into a preview card. Use this instead of pasting a bare
    URL: "PR #715" reads better than forty characters of github.com path, and carries the same click.
    """
    return "[%s](<%s>)" % (text, url)


def suppress_previews(text):
    """Wrap bare URLs in <> so Discord does not expand them into preview cards.

    Leaves alone anything already wrapped, any markdown link's target, and anything inside a code
    span or fence — where Discord renders no preview anyway and angle brackets would show up as
    literal characters in the code.
    """
    out, i = [], 0
    # Split on code fences and spans first; only rewrite what falls outside them.
    for m in re.finditer(r"```.*?```|`[^`\n]*`", text or "", re.DOTALL):
        out.append(_BARE_URL.sub(_wrap, text[i:m.start()]))
        out.append(m.group(0))
        i = m.end()
    out.append(_BARE_URL.sub(_wrap, (text or "")[i:]))
    return "".join(out)


def post(text, channel_id=None, reply_to=None):
    """Post a message to the channel (auto-chunked to <2000). reply_to threads it under that message.
    Returns the last created message dict (has 'id'). Mentions are disabled (safe in a shared server)."""
    tok, chan = _env(); chan = str(channel_id or chan or "")
    if not tok or not chan:
        raise SystemExit("discord_api: missing DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID")
    url = "%s/channels/%s/messages" % (API, chan)
    hdr = {"Authorization": "Bot %s" % tok, "Content-Type": "application/json", "User-Agent": UA}
    text = suppress_previews(text)
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
