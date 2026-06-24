#!/usr/bin/env python3
"""The single Telegram transport for all moprox agents.

Every outbound message — coach session reads, dev acks, steward chat replies, chart captions —
goes through here. Two reasons:
  1. one place owns creds + the hand-rolled multipart (no `requests` dependency);
  2. we enforce the convention that an agent's messages are prefixed with its handle (#coach,
     #dev, #steward). Pass `agent=` and the tag is prepended unless the text already starts with it.

Creds from ~/.config/claude-dev/telegram.env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).
"""
import json, os, urllib.parse, urllib.request
from pathlib import Path

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

def send(text, agent=None, reply_to=None):
    tok, chat = creds()
    p = {"chat_id": chat, "text": tag(text, agent)[:4000]}
    if reply_to: p["reply_to_message_id"] = reply_to
    return json.load(urllib.request.urlopen(API % (tok, "sendMessage") + "?" + urllib.parse.urlencode(p), timeout=20))

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
    return json.load(urllib.request.urlopen(req, timeout=30))
