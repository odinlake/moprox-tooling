#!/usr/bin/env python3
"""The single Telegram transport for all moprox agents.

Every outbound message — coach session reads, dev acks, steward chat replies, chart captions —
goes through here. Two reasons:
  1. one place owns creds + the hand-rolled multipart (no `requests` dependency);
  2. we enforce the convention that an agent's messages are prefixed with its handle (#coach,
     #dev, #steward). Pass `agent=` and the tag is prepended unless the text already starts with it.

Creds from ~/.config/claude-dev/telegram.env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).
"""
import html, json, os, re, urllib.error, urllib.parse, urllib.request
from pathlib import Path

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

def _table(rows_raw):
    """A markdown table block -> an aligned monospace <pre> (Telegram has no real tables)."""
    rows = []
    for ln in rows_raw:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if re.fullmatch(r"[\s:\-|]+", ln.strip()): continue      # the |---|---| separator row
        rows.append(cells)
    if not rows: return ""
    ncol = max(len(r) for r in rows); rows = [r + [""] * (ncol - len(r)) for r in rows]
    w = [max(len(r[i]) for r in rows) for i in range(ncol)]
    body = "\n".join(" │ ".join(c.ljust(w[i]) for i, c in enumerate(r)) for r in rows)
    return "<pre>" + html.escape(body) + "</pre>"

def md_to_html(text):
    """Convert the agents' markdown to the subset of HTML Telegram renders. Headings -> bold,
    tables -> monospace block, plus bold/italic/code; everything else is escaped to literal text."""
    holds = []
    def hold(frag): holds.append(frag); return "\x00%d\x00" % (len(holds) - 1)
    text = re.sub(r"```[^\n]*\n(.*?)```", lambda m: hold("<pre>" + html.escape(m.group(1).rstrip("\n")) + "</pre>"),
                  text, flags=re.S)
    # group consecutive '|' lines that include a separator row into one table block
    lines = text.split("\n"); out = []; i = 0
    while i < len(lines):
        if "|" in lines[i]:
            j = i
            while j < len(lines) and "|" in lines[j]: j += 1
            run = lines[i:j]
            if len(run) >= 2 and any(re.fullmatch(r"[\s:\-|]+", r.strip()) for r in run):
                out.append(hold(_table(run))); i = j; continue
        out.append(lines[i]); i += 1
    text = "\n".join(out)
    text = re.sub(r"`([^`]+)`", lambda m: hold("<code>" + html.escape(m.group(1)) + "</code>"), text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",         # [label](url) -> <a href>
                  lambda m: hold('<a href="%s">%s</a>' % (html.escape(m.group(2), quote=True), html.escape(m.group(1)))),
                  text)
    text = html.escape(text)                                     # escape the remaining plain text
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*#*$", r"<b>\1</b>", text)   # ATX headings -> bold
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", text)           # bullets
    text = re.sub(r"\x00(\d+)\x00", lambda m: holds[int(m.group(1))], text)
    return text

def _post(tok, method, params):
    return json.load(urllib.request.urlopen(API % (tok, method) + "?" + urllib.parse.urlencode(params), timeout=20))

def send(text, agent=None, reply_to=None):
    tok, chat = creds()
    body = tag(text, agent)
    p = {"chat_id": chat, "text": md_to_html(body)[:4096], "parse_mode": "HTML",
         "disable_web_page_preview": "true"}             # links stay inline; no big preview cards
    if reply_to:
        p["reply_to_message_id"] = reply_to
        p["allow_sending_without_reply"] = "true"               # stale/deleted target -> still send
    try:
        r = _post(tok, "sendMessage", p)
    except urllib.error.HTTPError:                               # bad HTML -> plain text, drop the reply target
        for k in ("parse_mode", "reply_to_message_id"): p.pop(k, None)
        p["text"] = body[:4096]
        r = _post(tok, "sendMessage", p)
    convo.log_out(agent or "system", body, (r.get("result") or {}).get("message_id"))
    return r

def send_photo(png, caption="", agent=None):
    tok, chat = creds()
    cap = tag(caption, agent)
    boundary = "----moprox%d" % (len(png) & 0xffffff)
    body = b""
    for k, v in (("chat_id", chat), ("caption", cap)):
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode()
    body += ("--%s\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"hr.png\"\r\nContent-Type: image/png\r\n\r\n" % boundary).encode()
    body += png + ("\r\n--%s--\r\n" % boundary).encode()
    req = urllib.request.Request(API % (tok, "sendPhoto"), data=body,
                                 headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    # One retry: a photo is the only thing the athlete actually sees, and a transient blip behind
    # Squid (timeout / 5xx) shouldn't drop it silently the way it did on 2026-06-24.
    last = {"ok": False, "description": "send_photo: not attempted"}
    for attempt in (1, 2):
        try:
            r = json.load(urllib.request.urlopen(req, timeout=30))
            convo.log_out(agent or "system", cap or "[chart]", (r.get("result") or {}).get("message_id"))
            return r
        except urllib.error.HTTPError as e:
            try: last = json.load(e)
            except Exception: last = {"ok": False, "description": "HTTP %s" % e.code}
        except Exception as e:
            last = {"ok": False, "description": "%s: %s" % (type(e).__name__, e)}
    return last
