#!/usr/bin/env python3
"""Pull the latest phone location from the relay into location.json.

Relay is configured in ~/.config/claude-dev/location-relay.json — either {"ntfy_topic": ".."} (the
iOS Shortcut POSTs a tiny message to a secret ntfy topic) or {"gist_id": ".."} (PATCHes a gist).
We just pull the latest payload and extract the first two numbers as lat/lon — deliberately forgiving,
so it copes with plain "lat,lon", JSON, brackets, or iOS smart-quote mangling. Latest-wins vs the
manual Telegram share (same file).
"""
import calendar, json, re, subprocess, time, urllib.request
from pathlib import Path

CFG = Path.home() / ".config/claude-dev/location-relay.json"
LOCATION = Path.home() / ".local/share/moprox/location.json"

def _two_floats(s):
    nums = re.findall(r"-?\d+\.\d+|-?\d+", s or "")
    return (float(nums[0]), float(nums[1])) if len(nums) >= 2 else None

def _write_if_newer(lat, lon, ts, source):
    cur = {}
    try: cur = json.loads(LOCATION.read_text())
    except Exception: pass
    if ts <= cur.get("ts", 0):
        print("relay: not newer; keeping current"); return cur
    rec = {"lat": lat, "lon": lon, "ts": ts, "source": source}
    LOCATION.parent.mkdir(parents=True, exist_ok=True); LOCATION.write_text(json.dumps(rec))
    print("location <- %s relay:" % source, lat, lon, "@", time.strftime("%H:%M", time.localtime(ts)))
    return rec

def pull_ntfy(topic):
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
    m = msgs[-1]; ll = _two_floats(m.get("message", ""))
    if not ll:
        print("ntfy: no coords in message:", (m.get("message") or "")[:80]); return None
    return _write_if_newer(ll[0], ll[1], int(m.get("time", time.time())), "ntfy")

def pull_gist(gid, fname):
    r = subprocess.run(["gh", "api", "/gists/%s" % gid], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        print("gist fetch failed:", (r.stderr or "")[:200]); return None
    g = json.loads(r.stdout)
    ll = _two_floats((g.get("files", {}).get(fname) or {}).get("content", ""))
    if not ll:
        print("gist: no coords yet"); return None
    ts = calendar.timegm(time.strptime(g["updated_at"], "%Y-%m-%dT%H:%M:%SZ"))
    return _write_if_newer(ll[0], ll[1], ts, "gist")

def pull():
    cfg = json.loads(CFG.read_text())
    if cfg.get("ntfy_topic"): return pull_ntfy(cfg["ntfy_topic"])
    if cfg.get("gist_id"):    return pull_gist(cfg["gist_id"], cfg.get("file", "moprox-location.json"))
    print("relay: config has neither ntfy_topic nor gist_id"); return None

if __name__ == "__main__":
    pull()
