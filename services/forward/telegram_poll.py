#!/usr/bin/env python3
"""Long-poll Telegram getUpdates and capture inbound messages into a durable inbox (JSONL) for a
router/steward to consume. ~30s long-poll loop (returns instantly on a message), so it's cheap and
near-real-time without webhooks. Creds from ~/.config/claude-dev/telegram.env.

This is just the *capture* layer — routing replies to the right agent is layered on top (see the
agent-architecture decision in docs/roadmap.md). Run as a Restart=always systemd service.
"""
import json, os, time, urllib.parse, urllib.request
from pathlib import Path

TG_ENV = Path(os.environ.get("TELEGRAM_ENV", Path.home() / ".config/claude-dev/telegram.env"))
INBOX  = Path(os.environ.get("TELEGRAM_INBOX", Path.home() / ".local/share/moprox/telegram-inbox.jsonl"))
STATE  = Path.home() / ".local/share/moprox/telegram-offset"
LOCATION = Path.home() / ".local/share/moprox/location.json"   # freshest fix from a shared/live location
# capture-only: the dispatcher service tails this inbox and does triage + routing (single-flight
# per agent), so a long agent run never blocks message pickup here. Location pins / Live Location
# updates are captured separately to LOCATION (latest wins) for the valet's "where am I" check.

def capture_location(loc):
    rec = {"lat": loc["latitude"], "lon": loc["longitude"], "ts": int(time.time()),
           "accuracy": loc.get("horizontal_accuracy"), "live_period": loc.get("live_period"),
           "heading": loc.get("heading")}
    if loc.get("live_period"): rec["until"] = rec["ts"] + loc["live_period"]
    LOCATION.parent.mkdir(parents=True, exist_ok=True)
    LOCATION.write_text(json.dumps(rec))
    print("location <-", rec["lat"], rec["lon"], "(live)" if loc.get("live_period") else "(pin)")

def creds():
    tok = chat = None
    for ln in TG_ENV.read_text().splitlines():
        if ln.startswith("TELEGRAM_BOT_TOKEN="): tok = ln.split("=", 1)[1].strip()
        if ln.startswith("TELEGRAM_CHAT_ID="):  chat = ln.split("=", 1)[1].strip()
    if not tok: raise SystemExit("missing TELEGRAM_BOT_TOKEN")
    return tok, chat

def api(tok, method, params):
    url = "https://api.telegram.org/bot%s/%s?%s" % (tok, method, urllib.parse.urlencode(params))
    return json.load(urllib.request.urlopen(url, timeout=45))

def main():
    tok, _ = creds()
    INBOX.parent.mkdir(parents=True, exist_ok=True)
    offset = int(STATE.read_text()) if STATE.exists() else 0
    print(f"telegram-poll up; offset={offset}; inbox={INBOX}")
    while True:
        try:
            r = api(tok, "getUpdates", {"offset": offset, "timeout": 30,
                                        "allowed_updates": json.dumps(["message", "edited_message"])})
        except Exception as e:
            print("poll error:", e); time.sleep(5); continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            m = u.get("message") or u.get("edited_message")    # live location streams as edited_message
            if not m: continue
            if m.get("location"):                              # a pin or Live Location update
                capture_location(m["location"]); continue
            if "message" not in u: continue                    # ignore edits to text; only new messages route
            rec = {"ts": int(time.time()), "update_id": u["update_id"], "chat_id": m["chat"]["id"],
                   "msg_id": m.get("message_id"),
                   "from": (m.get("from") or {}).get("username") or (m.get("from") or {}).get("first_name"),
                   "text": m.get("text", ""),
                   "reply_to": (m.get("reply_to_message") or {}).get("message_id")}
            with open(INBOX, "a") as f: f.write(json.dumps(rec) + "\n")
            print("inbox <-", rec["from"], repr(rec["text"][:80]))
        if r.get("result"): STATE.write_text(str(offset))

if __name__ == "__main__":
    main()
