#!/usr/bin/env python3
"""Build public-safe training data for the dashboard from a Polar Flow export.

Reads the raw export ZIP from the PRIVATE store and classifies each running session with the
shared analysis library (services/training/analysis) on the FULL per-second HR — never the
downsampled trace. Emits ONE public-safe JSON: per-session physiological type, HR stats, interval
structure, and a downsampled pure-HR trace (no GPS/names).

  POLAR_RAW   dir holding the Polar export *.zip   (default: ../private-data/polar/raw)
  OUT         output JSON path                     (default: ./dist/data/training/sessions.json)
"""
import glob, json, os, sys, time, zipfile
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analysis import Athlete, analyse_safe   # the validated engine

RUN_SPORTS = {1, 17, 83}
ATH = Athlete()                  # max_hr=202, resting=45, lt1=155, lt2=180 (calibrated in-chat)
TRACE_POINTS = 120               # classified sessions (detail chart; the rich chart is the Telegram one)
TRACE_POINTS_THIN = 50           # unknown / very short

def hr_series(d):
    ex = (d.get("exercises") or [{}])[0]
    best = []
    for s in (ex.get("samples") or {}).get("samples", []):
        if s.get("type") == "HEART_RATE":
            vals = [float(v) for v in (s.get("values") or []) if v and 30 < v < 220]
            if len(vals) > len(best): best = vals
    return best

def downsample(x, n):
    x = np.asarray(x, float)
    if len(x) <= n: return [round(float(v)) for v in x]
    step = len(x) / n
    return [round(float(np.mean(x[int(i*step):max(int(i*step)+1, int((i+1)*step))]))) for i in range(n)]

def build(raw_dir, out_path):
    zips = sorted(glob.glob(os.path.join(raw_dir, "*.zip")))
    if not zips: sys.exit(f"no export zip in {raw_dir}")
    sessions = []
    with zipfile.ZipFile(zips[-1]) as z:
        for nm in z.namelist():
            if "training-session_" not in nm or not nm.endswith(".json"): continue
            try: d = json.loads(z.read(nm))
            except Exception: continue
            try: sid = int((d.get("sport") or {}).get("id"))
            except Exception: continue
            if sid not in RUN_SPORTS: continue
            hr = hr_series(d)
            if len(hr) < 60: continue
            sport = d.get("name") or ""
            try:
                res = analyse_safe(hr, len(hr) / 60.0, ATH, sport)
            except Exception:
                continue
            cls = res["classification"]
            block = np.asarray(res["block"], float)
            block = np.clip(block, 40, 210)                  # display-clean the trace only
            cat = cls.session_type
            m5 = res.get("five_min_max")
            max5 = round(float(m5)) if m5 == m5 else round(float(np.nanmax(block)))  # m5!=m5 => NaN

            reps = []
            if cat in ("speed", "vo2max") and len(res.get("peaks_min", [])):
                blen = max(1.0, len(block))
                troughs = list(zip(res.get("troughs_min", []), res.get("troughs_hr", [])))
                for pm, ph in zip(res["peaks_min"], res["peaks_hr"]):
                    pt = pm * 60.0
                    before = [th for tm, th in troughs if tm < pm]
                    reps.append({"t": round(pt / blen, 3), "peak": round(float(ph)),
                                 "trough": round(float(before[-1])) if before else round(float(np.min(block))),
                                 "work_s": 0})
            tp = TRACE_POINTS if cat in ("easy", "tempo", "speed", "vo2max", "trail_easy") else TRACE_POINTS_THIN
            sessions.append(dict(
                id=(d.get("identifier") or {}).get("id", "")[:8],
                date=d.get("startTime", "")[:19], sport=sid, cat=cat,
                dur_min=round(len(hr) / 60.0, 1), hr_avg=round(float(np.mean(block))),
                hr_max=round(float(np.max(block))), max5=max5, nint=cls.n_work_bouts,
                above_lt2=bool(cls.above_lt2), clamp=bool(cls.hr_clamp_suspected),
                reps=reps,
                trace=downsample(block, tp), trace_step_s=round(len(block) / min(len(block), tp)),
            ))
    sessions.sort(key=lambda s: s["date"], reverse=True)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    json.dump({"generated": int(time.time()), "count": len(sessions), "sessions": sessions},
              open(out_path, "w"), separators=(",", ":"))
    by = {}
    for s in sessions: by[s["cat"]] = by.get(s["cat"], 0) + 1
    print(f"wrote {len(sessions)} sessions -> {out_path}")
    print("  " + "  ".join(f"{k}:{v}" for k, v in sorted(by.items())))

if __name__ == "__main__":
    raw = os.environ.get("POLAR_RAW", os.path.expanduser("~/projects/private-data/polar/raw"))
    out = os.environ.get("OUT", "dist/data/training/sessions.json")
    build(raw, out)
