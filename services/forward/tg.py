#!/usr/bin/env python3
"""The single Telegram transport for all moprox agents.

Every outbound message — coach session reads, dev acks, steward chat replies, chart captions —
goes through here. Two reasons:
  1. one place owns creds, the hand-rolled multipart (no `requests`), and the markdown->Telegram
     conversion, so formatting is identical for a text message and a photo caption;
  2. we enforce the convention that an agent's messages are prefixed with its handle (#coach,
     #dev, #steward). Pass `agent=` and the tag is prepended unless the text already starts with it.

Markdown is converted to Telegram **MarkdownV2** by the battle-tested `telegramify-markdown` (handles
the MarkdownV2 escaping + renders tables as an aligned monospace code block). Installed in the user
site (`pip --prefix=~/.local`); if it ever can't convert, we fall back to sending plain text.

Creds from ~/.config/claude-dev/telegram.env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).
"""
import json, os, re, urllib.error, urllib.parse, urllib.request
from pathlib import Path

import telegramify_markdown as tgmd   # markdown -> Telegram MarkdownV2 (user-site install)
import convo   # shared conversation log (records every outbound message + its msg_id)

TG_ENV = Path(os.environ.get("TELEGRAM_ENV", Path.home() / ".config/claude-dev/telegram.env"))
API = "https://api.telegram.org/bot%s/%s"

def creds():
    tok = os.environ.get("TELEGRAM_BOT_TOKEN"); chat = os.environ.get("TELEGRAM_CHAT_ID")
    if (not tok or not chat) and TG_ENV.exists():
        for ln in TG_ENV.read_text().splitlines():
            if ln.startswith("TELEGRAM_BOT_TOKEN="): tok = ln.split("=", 1)[1].strip()
            if ln.startswith("TELEGRAM_CHAT_ID="):  chat = ln.split("=", 1)[1].strip()
    if not tok or not chat: raise SystemExit("missing telegram creds (TELEGRAM_BOT_TOKEN/CHAT_ID)")
    return tok, chat

def tag(text, agent):
    """Prefix `text` with the agent's #handle unless it's already there. agent=None -> unchanged."""
    if not agent: return text
    h = "#" + str(agent).lstrip("#")
    return text if (text or "").lstrip().startswith(h) else "%s %s" % (h, text)

_BOLD_HEADER = re.compile(r"\*[^*\n]+\*:?")   # a line that is ONLY a bold span (opt. trailing colon)

def _md(text):
    """Convert agents' markdown to Telegram MarkdownV2 (bold/italic/code/links + tables->monospace
    code block, with all the fiddly MarkdownV2 escaping handled). Returns None on any failure so the
    caller falls back to sending plain text.

    Style — 'structural spacing'. Telegram has NO sub-line spacing: a gap is binary (single newline =
    none, blank line = a full line-height), there's no half. So instead of spacing everything (airy) or
    nothing (cramped), we mix: prose paragraphs are tight (single newline), and a blank line is kept
    only before a real section boundary — a standalone *bold header* line, or a table/code block. That
    reads as the middle ground. Code fences are preserved verbatim so table alignment survives."""
    # Drop horizontal-rule lines (---, ***, ___) BEFORE conversion: in CommonMark a line of text
    # followed by `---` is a *setext heading*, which MarkdownV2 renders as underlined bold and mangles
    # a whole block. Agents shouldn't emit rules for Telegram anyway. Table separators (`--- | ---`,
    # which contain `|`) and content inside ``` fences are left alone.
    src, in_f = [], False
    for ln in (text or "").split("\n"):
        if ln.lstrip().startswith("```"): in_f = not in_f
        if not in_f and re.fullmatch(r"[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*", ln): continue
        src.append(ln)
    try:
        md = tgmd.markdownify("\n".join(src))
    except Exception:
        return None
    out, in_fence = [], False
    for ln in md.split("\n"):
        s = ln.strip()
        if s.startswith("```"):                       # table/code block: set it off with a blank line
            if not in_fence and out and out[-1] != "": out.append("")
            out.append(ln); in_fence = not in_fence
            if not in_fence: out.append("")           # ...and after it closes
            continue
        if in_fence:
            out.append(ln); continue                  # preserve code content verbatim
        if s == "":
            continue                                  # tight: drop blank lines between prose paragraphs
        if _BOLD_HEADER.fullmatch(s) and out and out[-1] != "":
            out.append("")                            # keep a blank line before a standalone bold header
        out.append(ln)
    while out and out[-1] == "": out.pop()            # no trailing blank
    return "\n".join(out)

def _post(tok, method, params):
    return json.load(urllib.request.urlopen(API % (tok, method) + "?" + urllib.parse.urlencode(params), timeout=20))

def send(text, agent=None, reply_to=None):
    tok, chat = creds()
    body = tag(text, agent)
    md = _md(body)                                               # MarkdownV2, or None -> plain fallback
    p = {"chat_id": chat, "disable_web_page_preview": "true"}    # links stay inline; no big preview cards
    p["text"], p["parse_mode"] = (md, "MarkdownV2") if md is not None else (body[:4096], None)
    if p["parse_mode"] is None: p.pop("parse_mode")
    if reply_to:
        p["reply_to_message_id"] = reply_to
        p["allow_sending_without_reply"] = "true"               # stale/deleted target -> still send
    try:
        r = _post(tok, "sendMessage", p)
    except urllib.error.HTTPError:                              # bad entities / too long -> plain, drop reply target
        for k in ("parse_mode", "reply_to_message_id"): p.pop(k, None)
        p["text"] = body[:4096]
        r = _post(tok, "sendMessage", p)
    convo.log_out(agent or "system", body, (r.get("result") or {}).get("message_id"))
    return r

def _photo_body(chat, png, caption, parse_mode):
    """Build the hand-rolled multipart body for sendPhoto (caption + optional parse_mode)."""
    boundary = "----moprox%d" % (len(png) & 0xffffff)
    fields = [("chat_id", chat), ("caption", caption)]
    if parse_mode: fields.append(("parse_mode", parse_mode))
    body = b""
    for k, v in fields:
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode()
    body += ("--%s\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"hr.png\"\r\nContent-Type: image/png\r\n\r\n" % boundary).encode()
    body += png + ("\r\n--%s--\r\n" % boundary).encode()
    return body, boundary

def send_photo(png, caption="", agent=None):
    tok, chat = creds()
    cap = tag(caption, agent)
    # The caption gets the SAME MarkdownV2 formatting as a text message (bold, tables->monospace, code,
    # links). This path used to send the caption raw, so once the whole session read moved into the
    # caption it shipped literal **asterisks** and an unformatted pipe-table. Telegram caps a photo
    # caption at 1024 chars (visible, post-parse). MarkdownV2 first; fall back to plain on a parse error.
    md = _md(cap)
    variants = ([(md, "MarkdownV2")] if md is not None else []) + [(cap[:1024], None)]
    last = {"ok": False, "description": "send_photo: not attempted"}
    for caption_value, parse_mode in variants:
        body, boundary = _photo_body(chat, png, caption_value, parse_mode)
        req = urllib.request.Request(API % (tok, "sendPhoto"), data=body,
                                     headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
        # One retry per variant: a photo is the only thing the athlete actually sees, and a transient
        # blip behind Squid (timeout / 5xx) shouldn't drop it silently the way it did on 2026-06-24.
        for attempt in (1, 2):
            try:
                r = json.load(urllib.request.urlopen(req, timeout=30))
                convo.log_out(agent or "system", cap or "[chart]", (r.get("result") or {}).get("message_id"))
                return r
            except urllib.error.HTTPError as e:                  # bad entities / caption too long ->
                try: last = json.load(e)                         # don't retry same body, try next variant
                except Exception: last = {"ok": False, "description": "HTTP %s" % e.code}
                break
            except Exception as e:                               # transient -> retry once, then next variant
                last = {"ok": False, "description": "%s: %s" % (type(e).__name__, e)}
    return last
