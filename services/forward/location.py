#!/usr/bin/env python3
"""Where is the operator? Resolve the freshest location fix (captured by telegram_poll from a shared
pin / Live Location) against a configured HOME, and label it for the valet's brief so the operator
can verify it was picked up. Falls back to HOME when there's no recent fix.

  location.py            # print the currently-resolved location (debug / the "mention it" check)

HOME lives at ~/.config/claude-dev/home-location.json : {"lat":..,"lon":..,"name":".."}
"""
import json, math, sys, time, urllib.request
from pathlib import Path

LOCATION = Path.home() / ".local/share/moprox/location.json"
HOME     = Path.home() / ".config/claude-dev/home-location.json"
FRESH_H = 16          # a fix older than this (and not in an active live window) -> assume home
AWAY_KM = 5           # farther than this from home -> "away"

def _haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(h))

def home():
    try: return json.loads(HOME.read_text())
    except Exception: return None

def _fresh_fix():
    try: f = json.loads(LOCATION.read_text())
    except Exception: return None
    now = time.time()
    if (f.get("until") and now < f["until"]) or (now - f.get("ts", 0)) < FRESH_H*3600:
        return f
    return None

def reverse_geocode(lat, lon):
    url = ("https://api.bigdatacloud.net/data/reverse-geocode-client"
           "?latitude=%s&longitude=%s&localityLanguage=en" % (lat, lon))
    try:
        d = json.load(urllib.request.urlopen(url, timeout=15))
        return d.get("city") or d.get("locality") or d.get("principalSubdivision") or d.get("countryName")
    except Exception:
        return None

def resolve():
    """{lat, lon, name, status: home|away|home-default, ts} — or None if nothing is known yet."""
    h = home(); fix = _fresh_fix()
    if fix:
        lat, lon = fix["lat"], fix["lon"]
        away = (not h) or _haversine(lat, lon, h["lat"], h["lon"]) > AWAY_KM
        name = (reverse_geocode(lat, lon)
                or (h.get("name") if (h and not away) else None) or "%.3f,%.3f" % (lat, lon))
        return {"lat": lat, "lon": lon, "name": name, "status": "away" if away else "home", "ts": fix.get("ts")}
    if h:
        return {"lat": h["lat"], "lon": h["lon"], "name": h.get("name") or reverse_geocode(h["lat"], h["lon"]),
                "status": "home-default", "ts": None}
    return None

def label(r=None):
    r = r or resolve()
    if not r: return "📍 location unknown — set home"
    when = " (as of %s)" % time.strftime("%H:%M", time.localtime(r["ts"])) if (r["status"] == "away" and r.get("ts")) else ""
    tag = "away —" if r["status"] == "away" else "home"
    return "📍 %s %s%s" % (tag, r["name"], when)

if __name__ == "__main__":
    r = resolve(); print(label(r)); print(json.dumps(r) if r else "(no location and no home set)")
