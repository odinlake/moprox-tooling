#!/usr/bin/env python3
"""Polar fetcher: pull new exercises via AccessLink, and for sessions with >=10 min of HR, run the
coach + post a chart + commentary to Telegram. Store raw to private-data/polar/incoming so the
dashboard updater picks it up. Run on a timer; an empty transaction (204) exits in one request.

GATE: the coach is invoked ONLY when there are >=10 min of HR data (MIN_HR_SECONDS).

NOTE: the exact AccessLink sample endpoint/shape (extract_hr) is verified against the first real
exercise — flagged below. Everything else (transaction flow, gate, coach, post, commit) is solid.
"""
import json, os, re, sys, time, urllib.error, urllib.request
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path.home() / "projects/moprox-tooling/services/agents"))
sys.path.insert(0, str(Path.home() / "projects/moprox-tooling/services/forward"))
sys.path.insert(0, str(Path.home() / "projects/moprox-tooling/services/training"))
from run import run_agent
import telegram_feed
import route
from analysis import Athlete, analyse_safe

POLAR_ENV = Path.home() / ".config/claude-dev/polar.env"
INCOMING  = Path.home() / "projects/private-data/polar/incoming"
MIN_HR_SECONDS = 600          # 10 min — the coach gate
BASE = "https://www.polaraccesslink.com"
ATH = Athlete()

def env():
    d = {}
    for ln in POLAR_ENV.read_text().splitlines():
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1); d[k] = v.strip()
    return d

def api(path_or_url, tok, method="GET"):
    url = path_or_url if path_or_url.startswith("http") else BASE + path_or_url
    req = urllib.request.Request(url, method=method,
                                 headers={"Authorization": "Bearer " + tok, "Accept": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=40)
        return r.status, (json.load(r) if r.status == 200 else None)
    except urllib.error.HTTPError as e:
        return e.code, None

def iso_minutes(dur):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", dur or "")
    if not m: return 0.0
    h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    return h * 60 + mi + s / 60.0

def extract_hr(ex_url, tok):
    """Per-second HR for an exercise. AccessLink: GET {ex}/samples -> sample urls; each ->
    {sample-type, recording-rate, data:'v,v,v'}; type '0' == heart rate. *** verify on 1st run ***"""
    st, lst = api(ex_url + "/samples", tok)
    if st != 200 or not lst: return []
    for sample_url in lst.get("samples", []):
        st2, s = api(sample_url, tok)
        if st2 == 200 and s and str(s.get("sample-type")) == "0":
            data = s.get("data", "")
            return [float(v) for v in data.split(",") if v and v != "null"]
    return []

def handle_exercise(uid, ex_url, tok):
    st, ex = api(ex_url, tok)
    if st != 200 or not ex: return None
    dur_min = iso_minutes(ex.get("duration"))
    hr = extract_hr(ex_url, tok)
    # store raw regardless (durable; dashboard integration of incoming is separate)
    INCOMING.mkdir(parents=True, exist_ok=True)
    eid = str(ex.get("id") or ex.get("start-time", "ex"))[:40]
    (INCOMING / ("exercise_%s.json" % re.sub(r"[^A-Za-z0-9_-]", "_", eid))).write_text(
        json.dumps({"summary": ex, "hr": hr}, separators=(",", ":")))
    # GATE: only invoke the coach for substantial HR sessions
    if len(hr) < MIN_HR_SECONDS:
        print("skip coach: %s only %.0f min HR (%d s)" % (eid, len(hr)/60.0, len(hr)))
        return ("short", eid)
    # analyse + build a session dict for the chart
    res = analyse_safe([h for h in hr if 30 < h < 220], len(hr)/60.0, ATH, ex.get("sport") or "")
    cls = res["classification"]; block = np.clip(np.asarray(res["block"], float), 40, 210)
    n = min(len(block), 120); step = len(block)/n
    trace = [round(float(np.mean(block[int(i*step):max(int(i*step)+1, int((i+1)*step))]))) for i in range(n)]
    reps = []
    if cls.session_type in ("speed", "vo2max"):
        blen = max(1.0, len(block))
        for pm, ph in zip(res.get("peaks_min", []), res.get("peaks_hr", [])):
            reps.append({"t": round(pm*60/blen, 3), "peak": round(float(ph)), "trough": 0, "work_s": 0})
    m5 = res.get("five_min_max")
    sess = dict(cat=cls.session_type, date=(ex.get("start-time") or "")[:19], trace=trace,
                trace_step_s=round(len(block)/n), dur_min=round(dur_min, 1),
                hr_avg=round(float(np.mean(block))), hr_max=round(float(np.max(block))),
                max5=round(float(m5)) if m5 == m5 else 0, nint=cls.n_work_bouts, reps=reps)
    # chart
    telegram_feed.send_photo(telegram_feed.plot_session(sess), telegram_feed.caption(sess))
    # coach commentary on the analysis
    summary = {k: sess[k] for k in ("cat", "date", "dur_min", "hr_avg", "hr_max", "max5", "nint")}
    summary["above_lt2"] = bool(cls.above_lt2); summary["clamp"] = bool(cls.hr_clamp_suspected)
    commentary = run_agent("coach",
        "New session just came in. Here is its computed analysis:\n%s\nWrite the session read in "
        "your voice (type + plan-match, the numbers, hedged interpretation, 1-2 takeaways)." % json.dumps(summary),
        timeout=420)
    route.send(commentary)          # the coach's session read as a follow-up message
    return ("posted", eid)

def main():
    e = env(); tok = e["POLAR_ACCESS_TOKEN"]; uid = e["POLAR_USER_ID"]
    st, tx = api("/v3/users/%s/exercise-transactions" % uid, tok, method="POST")
    if st == 204 or not tx:
        return                                   # nothing new — fast exit
    tid = tx.get("transaction-id") or tx.get("resource-uri", "").rstrip("/").split("/")[-1]
    st2, ex_list = api("/v3/users/%s/exercise-transactions/%s" % (uid, tid), tok)
    for ex_url in (ex_list or {}).get("exercises", []):
        try:
            print("exercise:", handle_exercise(uid, ex_url, tok))
        except Exception as ex:
            print("exercise error:", ex)
    api("/v3/users/%s/exercise-transactions/%s" % (uid, tid), tok, method="PUT")   # commit

if __name__ == "__main__":
    main()
