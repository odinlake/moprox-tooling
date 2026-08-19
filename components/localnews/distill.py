#!/usr/bin/env python3
"""Classify pending local-news posts (webscout reader) via claude -p on the Max sub.

Filters per operator prefs: DROP pets & opinion/chatter; KEEP crime/accident/incident;
discretionary council/events. Significance 0-5 weights proximity (near Tooting/Colliers
Wood > borough > farther). Prefetches full text for kept items so taps are instant."""
import json, os, shutil, subprocess, sys, urllib.request
from pathlib import Path

# Resolve the CLI ABSOLUTELY. Under systemd the service PATH is minimal and does not include
# ~/.local/bin, so a bare "claude" raises FileNotFoundError. That is exactly what happened here:
# Claude Code moved to the native installer at ~/.local/bin/claude, every classify() started
# throwing, the bare except below scored it 0, and the unit went on reporting success. Result:
# ~1,200 posts scored 0 across 21 days with nothing anywhere saying so.
CLAUDE = os.path.expanduser("~/.local/bin/claude")
if not os.path.exists(CLAUDE):
    CLAUDE = shutil.which("claude") or "claude"

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
    r = subprocess.run([CLAUDE, "-p", "--output-format", "text", "--model", "haiku"],
                       input=prompt, capture_output=True, text=True, timeout=120)
    txt = r.stdout.strip()
    txt = txt[txt.find("{"):txt.rfind("}") + 1]
    return json.loads(txt)


def main():
    pending = call("/api/pending")
    kept = 0
    failed = 0
    for p in pending:
        try:
            a = classify(p)
        except Exception as exc:
            # NEVER write a fabricated score. Scoring 0 on failure is indistinguishable from a
            # genuine "this post is noise", so a broken classifier looks exactly like a quiet
            # neighbourhood — which is how this went unnoticed for three weeks. Leave the post
            # pending instead: the next run retries it, and the backlog is itself the alarm.
            print(f"  ! classify failed for {p.get('id')}: {type(exc).__name__}: {exc}", flush=True)
            failed += 1
            continue
        a["id"] = p["id"]
        call("/api/annotate", a)
        if a.get("significance", 0) >= 3 and a.get("category") in ("crime", "accident", "incident", "council", "event"):
            kept += 1
            try:
                urllib.request.urlopen(BASE + "/p/" + p["id"], timeout=90).read()  # prefetch full text
            except Exception:
                pass
    ok = len(pending) - failed
    print(f"annotated={ok} failed={failed} brief-worthy={kept}", flush=True)
    # A total wipeout is a broken classifier, not a quiet day. Exit non-zero so the unit goes red,
    # which puts it in logview's incident queue instead of dying silently in a green log line.
    #
    # But "all of them" has to mean something. On 2026-08-17T20:50 this fired on 1/1 — a single
    # `claude -p` returned non-JSON, the batch happened to hold one post, and the unit went red for a
    # classifier that was fine on the next run and every run since. One sample cannot distinguish a
    # broken classifier from a flaky call, so below MIN_WIPEOUT this warns and exits clean. Nothing is
    # lost by waiting: a failed post is never annotated (see above), so it stays pending and the
    # backlog is still the alarm — it just takes a real wipeout to say so.
    MIN_WIPEOUT = 3
    if pending and ok == 0:
        if len(pending) >= MIN_WIPEOUT:
            sys.exit(f"every classification failed ({failed}/{len(pending)}) — classifier is broken")
        print(f"  ! all {failed} of this batch failed, below the {MIN_WIPEOUT}-post bar for calling "
              f"the classifier broken — left pending for the next run", flush=True)


if __name__ == "__main__":
    main()
