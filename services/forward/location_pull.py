#!/usr/bin/env python3
"""Pull the latest phone location from the relay gist into location.json.

The iOS Shortcut PATCHes a secret gist (~3am, silently) with {"lat":..,"lon":..}. We read it via gh,
using the gist's own `updated_at` as the fix timestamp so the phone side stays trivial. Only
overwrites location.json when newer (the manual Telegram share writes the same file — latest wins).
Gist id from ~/.config/claude-dev/location-relay.json.
"""
import calendar, json, subprocess, time
from pathlib import Path

CFG = Path.home() / ".config/claude-dev/location-relay.json"
LOCATION = Path.home() / ".local/share/moprox/location.json"

def pull():
    cfg = json.loads(CFG.read_text())
    gid, fname = cfg["gist_id"], cfg.get("file", "moprox-location.json")
    r = subprocess.run(["gh", "api", "/gists/%s" % gid], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        print("relay: gist fetch failed:", (r.stderr or "")[:200]); return None
    g = json.loads(r.stdout)
    content = (g.get("files", {}).get(fname) or {}).get("content")
    try: fix = json.loads(content) if content else {}
    except Exception: print("relay: content not JSON"); return None
    if not fix.get("lat"):
        print("relay: no fix yet"); return None
    ts = calendar.timegm(time.strptime(g["updated_at"], "%Y-%m-%dT%H:%M:%SZ"))   # gist mod time = fix time
    rec = {"lat": float(fix["lat"]), "lon": float(fix["lon"]), "ts": ts,
           "accuracy": fix.get("acc"), "source": "gist"}
    cur = {}
    try: cur = json.loads(LOCATION.read_text())
    except Exception: pass
    if ts <= cur.get("ts", 0):
        print("relay: not newer (ts %d <= %d); keeping current" % (ts, cur.get("ts", 0))); return cur
    LOCATION.parent.mkdir(parents=True, exist_ok=True)
    LOCATION.write_text(json.dumps(rec))
    print("location <- gist relay:", rec["lat"], rec["lon"], "@", g["updated_at"])
    return rec

if __name__ == "__main__":
    pull()
