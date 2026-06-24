#!/usr/bin/env python3
"""Steward routing: triage one inbound Telegram message and act on it.

Imported by the Telegram poller and called per message. The steward (a claude -p agent) decides
coach | dev | chat | ignore; we then run the coach for training, log dev requests for the dev
session, or send a chat reply. All agents run on the Max plan via run_agent.
"""
import json, os, re, sys, urllib.parse, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path.home() / "projects/moprox-tooling/services/agents"))
from run import run_agent

TG_ENV = Path.home() / ".config/claude-dev/telegram.env"
DEV_INBOX = Path.home() / ".local/share/moprox/dev-requests.jsonl"

def _creds():
    tok = chat = None
    for ln in TG_ENV.read_text().splitlines():
        if ln.startswith("TELEGRAM_BOT_TOKEN="): tok = ln.split("=", 1)[1].strip()
        if ln.startswith("TELEGRAM_CHAT_ID="):  chat = ln.split("=", 1)[1].strip()
    return tok, chat

def send(text, reply_to=None):
    tok, chat = _creds()
    p = {"chat_id": chat, "text": text[:4000]}
    if reply_to: p["reply_to_message_id"] = reply_to
    urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage?%s" % (tok, urllib.parse.urlencode(p)), timeout=20)

def _json(s):
    m = re.search(r"\{.*\}", s, re.S)
    try: return json.loads(m.group(0)) if m else None
    except Exception: return None

def process_message(rec):
    text = (rec.get("text") or "").strip()
    if not text: return
    prompt = ("Triage this inbound Telegram message from the athlete and output ONLY the routing "
              "JSON object (no prose, no code fence).\n\nMESSAGE: %s" % text)
    try:
        decision = _json(run_agent("steward", prompt, timeout=120)) or {"route": "ignore"}
    except Exception as e:
        send("(steward error: %s)" % str(e)[:150]); return
    route, ctx = decision.get("route", "ignore"), decision.get("context", "")
    print("route:", route, "|", text[:60])
    if route == "coach":
        reply = run_agent("coach",
            "The athlete sent this via Telegram: %r\nContext: %s\nReply concisely, in your voice." % (text, ctx),
            timeout=420)
        send(reply, reply_to=rec.get("reply_to") or None)
    elif route == "dev":
        DEV_INBOX.parent.mkdir(parents=True, exist_ok=True)
        with open(DEV_INBOX, "a") as f:
            f.write(json.dumps({"ts": rec.get("ts"), "text": text, "context": ctx}) + "\n")
        send("🛠️ Logged as a dev task — I'll pick it up in the next dev session.\n• " + (ctx or text))
    elif route == "chat":
        send(ctx or "👍")
    # ignore -> nothing

if __name__ == "__main__":
    process_message({"text": " ".join(sys.argv[1:])})
