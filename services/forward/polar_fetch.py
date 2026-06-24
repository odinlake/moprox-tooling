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
import numpy as np
sys.path.insert(0, str(Path.home() / "projects/moprox-tooling/services/agents"))
sys.path.insert(0, str(Path.home() / "projects/moprox-tooling/services/forward"))
sys.path.insert(0, str(Path.home() / "projects/moprox-tooling/services/training"))
from run import run_agent
import telegram_feed
import tg
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
    dur_min = len(hr) / 60.0
    res = analyse_safe([h for h in hr if 30 < h < 220], dur_min, ATH, ex.get("sport") or "")
    cls = res["classification"]; block = np.clip(np.asarray(res["block"], float), 40, 210)
    n = min(len(block), 120); step = len(block) / n
    trace = [round(float(np.mean(block[int(i*step):max(int(i*step)+1, int((i+1)*step))]))) for i in range(n)]
    reps = []
    if cls.session_type in ("speed", "vo2max"):
        blen = max(1.0, len(block))
        for pm, ph in zip(res.get("peaks_min", []), res.get("peaks_hr", [])):
            reps.append({"t": round(pm*60/blen, 3), "peak": round(float(ph)), "trough": 0, "work_s": 0})
    m5 = res.get("five_min_max")
    sess = dict(cat=cls.session_type, date=(ex.get("start_time") or "")[:19], trace=trace,
                trace_step_s=round(len(block)/n), dur_min=round(dur_min, 1),
                hr_avg=round(float(np.mean(block))), hr_max=round(float(np.max(block))),
                max5=round(float(m5)) if m5 == m5 else 0, nint=cls.n_work_bouts, reps=reps)
    if not telegram_feed.send_session(sess, agent="coach"):   # chart (caption #coach-tagged)
        print("polar: WARN chart send failed for %s — commentary still posting" % (ex.get("id") or "?"))
    summary = {k: sess[k] for k in ("cat", "date", "dur_min", "hr_avg", "hr_max", "max5", "nint")}
    summary["above_lt2"] = bool(cls.above_lt2); summary["clamp"] = bool(cls.hr_clamp_suspected)
    commentary = run_agent("coach",
        "New session just came in. Here is its computed analysis:\n%s\nWrite the session read in "
        "your voice (type + plan-match, the numbers, hedged interpretation, 1-2 takeaways)." % json.dumps(summary),
        timeout=420)
    tg.send(commentary, agent="coach")
    return sess["cat"]

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
