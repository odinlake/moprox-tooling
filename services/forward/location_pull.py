#!/usr/bin/env python3
"""Pull the latest phone location from the ntfy relay into location.json.

The iOS Shortcut publishes (~3am, silently) a tiny message to a secret ntfy topic — a FLAT JSON body
{"topic": "<secret>", "message": "{\\"lat\\":..,\\"lon\\":..}"} — which is all the Shortcuts
request-body builder can do. We poll ntfy for the latest message and parse its `message` as the fix,
using ntfy's message time as the timestamp. Only overwrites location.json when newer (the manual
Telegram share writes the same file — latest wins). Topic from ~/.config/claude-dev/location-relay.json.
"""
import json, time, urllib.request
from pathlib import Path

CFG = Path.home() / ".config/claude-dev/location-relay.json"
LOCATION = Path.home() / ".local/share/moprox/location.json"

def pull():
    topic = json.loads(CFG.read_text())["ntfy_topic"]
    url = "https://ntfy.sh/%s/json?poll=1&since=13h" % topic
    msgs = []
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            for line in r:
                try: m = json.loads(line)
                except Exception: continue
                if m.get("event") == "message": msgs.append(m)
    except Exception as e:
        print("ntfy: poll failed:", e); return None
    if not msgs:
        print("ntfy: no recent message"); return None
    latest = msgs[-1]
    try: fix = json.loads(latest.get("message", ""))
    except Exception: print("ntfy: message not JSON:", latest.get("message", "")[:80]); return None
    if not fix.get("lat"):
        print("ntfy: no fix in message"); return None
    ts = int(latest.get("time", time.time()))
    rec = {"lat": float(fix["lat"]), "lon": float(fix["lon"]), "ts": ts, "source": "ntfy"}
    cur = {}
    try: cur = json.loads(LOCATION.read_text())
    except Exception: pass
    if ts <= cur.get("ts", 0):
        print("ntfy: not newer; keeping current"); return cur
    LOCATION.parent.mkdir(parents=True, exist_ok=True)
    LOCATION.write_text(json.dumps(rec))
    print("location <- ntfy relay:", rec["lat"], rec["lon"], "@", time.strftime("%H:%M", time.localtime(ts)))
    return rec

if __name__ == "__main__":
    pull()
