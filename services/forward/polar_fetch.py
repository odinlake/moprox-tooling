#!/usr/bin/env python3
"""Polar fetcher: pull new exercises via AccessLink's direct **/v3/exercises** listing.

Why not exercise-transactions: that transaction model silently omits Polar Beat phone sessions
(verified live — it returns 204 while the workout exists). The direct listing returns every
exercise in Flow (device "Polar BEAT" included) and, with ?samples=true, the per-second HR inline.

For each new exercise with >=10 min of per-second HR we run the coach and post a chart + commentary
to Telegram. Every exercise's raw {summary, hr} is stored under private-data/polar/incoming so the
dashboard updater ingests it.

Dedup + no-history-spam: a seen-set (polar-seen.json) tracks processed ids. On a cold start the
90-day back-catalogue is stored raw and marked seen, but only exercises uploaded within
FRESH_WINDOW_H are actually posted — so today's workout posts while 90 days don't flood the DM.
"""
import calendar, json, re, sys, time, urllib.error, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path.home() / "projects/moprox-tooling/services/agents"))
sys.path.insert(0, str(Path.home() / "projects/moprox-tooling/services/forward"))
sys.path.insert(0, str(Path.home() / "projects/moprox-tooling/services/training"))
from run import run_agent
import convo
from analysis import Athlete, analyse_safe

POLAR_ENV = Path.home() / ".config/claude-dev/polar.env"
INCOMING  = Path.home() / "projects/private-data/polar/incoming"
SEEN      = Path.home() / ".local/share/moprox/polar-seen.json"
MIN_HR_SECONDS = 600          # 10 min — the coach gate
FRESH_WINDOW_H = 6            # only post exercises uploaded within this many hours (no history spam)
BASE = "https://www.polaraccesslink.com"
ATHLETE_JSON = Path.home() / "projects/private-data/agents/coach/athlete.json"
ATH = Athlete.load(ATHLETE_JSON)   # canonical physiology the coach owns (falls back to defaults)

def env():
    d = {}
    for ln in POLAR_ENV.read_text().splitlines():
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1); d[k] = v.strip()
    return d

def api(path, tok):
    url = path if path.startswith("http") else BASE + path
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok, "Accept": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=40)
        return r.status, (json.load(r) if r.status == 200 else None)
    except urllib.error.HTTPError as e:
        return e.code, None

def hr_from(ex):
    """Per-second HR from an exercise's inline samples (sample_type 0, recording_rate 1)."""
    for smp in ex.get("samples") or []:
        if str(smp.get("sample_type")) == "0" and int(smp.get("recording_rate") or 1) == 1:
            return [float(v) for v in (smp.get("data") or "").split(",") if v and v != "null"]
    return []

def load_seen():
    try: return set(json.loads(SEEN.read_text()))
    except Exception: return set()

def save_seen(s):
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    SEEN.write_text(json.dumps(sorted(s)))

def store_raw(ex, hr):
    INCOMING.mkdir(parents=True, exist_ok=True)
    fid = re.sub(r"[^A-Za-z0-9_-]", "_", str(ex.get("id") or ex.get("start_time", "ex"))[:40])
    (INCOMING / ("exercise_%s.json" % fid)).write_text(
        json.dumps({"summary": ex, "hr": hr}, separators=(",", ":")))

def upload_age_h(ex):
    up = (ex.get("upload_time") or "")[:19]            # e.g. 2026-06-24T11:40:40 (Z/UTC)
    try: return (time.time() - calendar.timegm(time.strptime(up, "%Y-%m-%dT%H:%M:%S"))) / 3600.0
    except Exception: return 0.0

def post_session(ex, hr):
    """A new workout came in. Coach OWNS the post: it builds its OWN light-mode chart and sends ONE
    Telegram message — the chart with the read written into its caption (its firm standing rule; no
    separate chart, no separate text). The pipeline no longer draws a chart or sends any commentary
    of its own; it just hands coach the computed analysis + the recent conversation (so coach applies
    the latest feedback) and lets the expert do the rest."""
    dur_min = len(hr) / 60.0
    res = analyse_safe([h for h in hr if 30 < h < 220], dur_min, ATH, ex.get("sport") or "")
    cls = res["classification"]; m5 = res.get("five_min_max")
    fid = re.sub(r"[^A-Za-z0-9_-]", "_", str(ex.get("id") or ex.get("start_time", "ex"))[:40])
    summary = {"exercise_id": ex.get("id"), "raw_file": str(INCOMING / ("exercise_%s.json" % fid)),
               "cat": cls.session_type, "date": (ex.get("start_time") or "")[:19],
               "dur_min": round(dur_min, 1), "n_work_bouts": cls.n_work_bouts,
               "five_min_max": round(float(m5)) if m5 == m5 else None,
               "above_lt2": bool(cls.above_lt2), "clamp": bool(cls.hr_clamp_suspected)}
    prompt = (
        "A new training session just came in. Computed classification (a hint — the raw per-second HR "
        "is in `raw_file` as {summary, hr}; recompute/refit from it as you see fit, you're the "
        "expert):\n%s\n\n"
        "Post it to Mikael the way you always do: build your light-mode chart per your standing "
        "per-type spec and send it YOURSELF (tg.send_photo) as ONE message with the whole read "
        "written into the caption. That single chart-with-caption post IS the entire reply — no "
        "separate chart, no separate text, no preamble or follow-up. Reuse and maintain your own "
        "charting library (see your CLAUDE.md), not throwaway /tmp scripts. Apply anything relevant "
        "from the recent conversation below.\n\nRecent conversation:\n%s"
        % (json.dumps(summary), convo.transcript(16)))
    run_agent("coach", prompt, timeout=600)   # coach sends its own single post; nothing else is sent
    return cls.session_type

def main():
    tok = env()["POLAR_ACCESS_TOKEN"]
    st, lst = api("/v3/exercises", tok)
    if st != 200 or lst is None:
        print("polar: /v3/exercises returned %s" % st); return
    seen = load_seen(); posted = 0
    print("polar: %d exercises listed; %d seen" % (len(lst), len(seen)))
    for summ in lst:
        eid = str(summ.get("id") or "")
        if not eid or eid in seen: continue
        st2, ex = api("/v3/exercises/%s?samples=true" % eid, tok)
        if st2 != 200 or not ex:
            print("polar: fetch %s -> %s" % (eid, st2)); continue
        hr = hr_from(ex); store_raw(ex, hr); seen.add(eid)
        mins = len(hr) / 60.0
        if len(hr) < MIN_HR_SECONDS:
            print("polar: %s stored, %.0f min HR — below coach gate" % (eid, mins)); continue
        if upload_age_h(ex) > FRESH_WINDOW_H:
            print("polar: %s stored (backfill) — not posting" % eid); continue
        try:
            cat = post_session(ex, hr); posted += 1
            print("polar: POSTED %s (%s, %s, %.0f min)" % (eid, ex.get("sport"), cat, mins))
        except Exception as e:
            print("polar: post error %s: %s" % (eid, e))
    save_seen(seen)
    print("polar: done; posted %d" % posted)

if __name__ == "__main__":
    main()
