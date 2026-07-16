#!/usr/bin/env python3
"""Classify pending local-news posts (webscout reader) via claude -p on the Max sub.

Filters per operator prefs: DROP pets & opinion/chatter; KEEP crime/accident/incident;
discretionary council/events. Significance 0-5 weights proximity (near Tooting/Colliers
Wood > borough > farther). Prefetches full text for kept items so taps are instant."""
import json, subprocess, urllib.request
from pathlib import Path

BASE = "http://10.10.10.8:8004"
TOKEN = (Path.home() / ".config/claude-dev/reader-token").read_text().strip()
NEAR = ["Colliers Wood", "Tooting", "West Tooting", "Tooting Bec", "Lavender Fields", "Merton"]
MID = ["Mitcham", "Wimbledon", "Earlsfield", "Summerstown", "Furzedown", "Balham",
       "Streatham", "Morden", "Pollards Hill", "Wandsworth"]

PROMPT = """Classify this Nextdoor post for a local-news brief. Reply ONLY with JSON:
{{"title": "<=60 chars, factual", "blurb": "<=160 chars, what happened", "category":
"crime|accident|incident|council|event|pets|opinion|chatter|photo|services|other",
"significance": 0-5, "keywords": ["k1","k2"]}}
significance: 5=serious crime/danger nearby, 4=notable incident/witnessed event, 3=council
action/local event worth knowing, 2=minor, 1=trivial, 0=noise. Proximity tiers: NEAR={near};
MID={mid}; other areas = farther, score lower. pets/opinion/chatter/photo always <=1.
POST (area: {area}, when: {when}): {body}"""


def call(path, data=None):
    req = urllib.request.Request(BASE + path, headers={"X-Reader-Token": TOKEN})
    if data is not None:
        req.data = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


def classify(p):
    prompt = PROMPT.format(near=", ".join(NEAR), mid=", ".join(MID),
                           area=p.get("area") or "?", when=p.get("when_rel") or "?",
                           body=(p.get("body") or "")[:900])
    r = subprocess.run(["claude", "-p", "--output-format", "text", "--model", "haiku"],
                       input=prompt, capture_output=True, text=True, timeout=120)
    txt = r.stdout.strip()
    txt = txt[txt.find("{"):txt.rfind("}") + 1]
    return json.loads(txt)


def main():
    pending = call("/api/pending")
    kept = 0
    for p in pending:
        try:
            a = classify(p)
        except Exception:
            a = {"title": (p.get("body") or "")[:60], "blurb": "", "category": "other",
                 "significance": 0, "keywords": []}
        a["id"] = p["id"]
        call("/api/annotate", a)
        if a.get("significance", 0) >= 3 and a.get("category") in ("crime", "accident", "incident", "council", "event"):
            kept += 1
            try:
                urllib.request.urlopen(BASE + "/p/" + p["id"], timeout=90).read()  # prefetch full text
            except Exception:
                pass
    print(f"annotated={len(pending)} brief-worthy={kept}")


if __name__ == "__main__":
    main()
