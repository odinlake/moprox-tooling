#!/usr/bin/env python3
"""Build public-safe training data for the dashboard from a Polar Flow export.

Reads the raw export ZIP straight from the PRIVATE store (never extracts GPS/names into the
output), classifies each running session, and writes ONE public-safe JSON: per-session category,
HR stats, interval structure, and a downsampled pure-HR trace. Pure stdlib.

  POLAR_RAW   dir holding the Polar export *.zip   (default: ../private-data/polar/raw)
  OUT         output JSON path                     (default: ./dist/data/training/sessions.json)

Categories (HR-driven, first cut — thresholds at top, tune freely):
  drop <30min; intervals = repeated swings reaching ~82% maxHR (>=7 reps speed, else vo2max);
  else high HR-roughness -> trail; else max-5min-avg <160 -> easy; else tempo.
"""
import glob, io, json, os, statistics as st, sys, time, zipfile

RUN_SPORTS = {1, 17, 83}
MIN_DURATION_MIN = 30
EASY_MAX5 = 160
TRAIL_ROUGHNESS = 4.5
SWING_AMP = 14
REP_HIGH_FRAC = 0.55
REP_MAXHR_FRAC = 0.82
INT_MIN_REPS = 3
SPEED_MIN_REPS = 7
TRACE_POINTS = 240          # downsample each HR trace to ~this many points for the chart

def median_filter(x, w=5):
    h = w//2
    return [st.median(x[max(0,i-h):i+h+1]) for i in range(len(x))]
def ma(x, w):
    if w<=1 or len(x)<w: return x[:]
    half, out = w//2, []
    for i in range(len(x)):
        lo, hi = max(0,i-half), min(len(x), i-half+w); out.append(sum(x[lo:hi])/(hi-lo))
    return out
def pct(x, p):
    if not x: return 0.0
    s=sorted(x); k=(len(s)-1)*p/100.0; f=int(k)
    return s[f] if f+1>=len(s) else s[f]+(k-f)*(s[f+1]-s[f])
def rolling_mean_max(x, w):
    if len(x)<w: return sum(x)/len(x) if x else 0
    s=sum(x[:w]); best=s/w
    for i in range(w,len(x)): s+=x[i]-x[i-w]; best=max(best,s/w)
    return best
def downsample(x, n):
    if len(x)<=n: return [round(v) for v in x]
    step=len(x)/n
    return [round(sum(x[int(i*step):int((i+1)*step)])/max(1,int((i+1)*step)-int(i*step))) for i in range(n)]

def zigzag(sm, amp):
    if len(sm)<3: return []
    piv=[]; direction=0; hi=lo=0
    for i,v in enumerate(sm):
        if v>sm[hi]: hi=i
        if v<sm[lo]: lo=i
        if direction>=0 and v<=sm[hi]-amp: piv.append(("H",hi,sm[hi])); direction=-1; lo=i
        elif direction<=0 and v>=sm[lo]+amp: piv.append(("L",lo,sm[lo])); direction=1; hi=i
    return piv

def intervals(sm, abs_high):
    base, peak = pct(sm,20), pct(sm,95)
    if peak-base < 18: return []
    high = max(abs_high, base + REP_HIGH_FRAC*(peak-base))
    piv = zigzag(sm, SWING_AMP); reps=[]
    for k,(t,idx,val) in enumerate(piv):
        if t=="H" and val>=high:
            prev_l = next((p for p in reversed(piv[:k]) if p[0]=="L"), None)
            next_l = next((p for p in piv[k+1:] if p[0]=="L"), None)
            lo_i = prev_l[1] if prev_l else 0; hi_i = next_l[1] if next_l else len(sm)-1
            work = sum(1 for v in sm[lo_i:hi_i] if v >= val-SWING_AMP)
            reps.append(dict(at=idx, peak=round(val), trough=round(prev_l[2]) if prev_l else round(base), work_s=work))
    return reps

def hr_series(d):
    ex=(d.get("exercises") or [{}])[0]; best=[]
    for s in (ex.get("samples") or {}).get("samples", []):
        if s.get("type")=="HEART_RATE":
            vals=[v for v in (s.get("values") or []) if v]
            if len(vals)>len(best): best=[float(v) for v in vals]
    return best

def classify(dur, nint, rough, max5):
    if dur < MIN_DURATION_MIN: return "dropped"
    if nint >= INT_MIN_REPS: return "speed" if nint >= SPEED_MIN_REPS else "vo2max"
    if rough >= TRAIL_ROUGHNESS: return "trail"
    return "easy" if max5 < EASY_MAX5 else "tempo"

def build(raw_dir, out_path):
    zips = sorted(glob.glob(os.path.join(raw_dir, "*.zip")))
    if not zips: sys.exit(f"no export zip in {raw_dir}")
    sessions = []
    with zipfile.ZipFile(zips[-1]) as z:                      # newest export wins
        for nm in z.namelist():
            if "training-session_" not in nm or not nm.endswith(".json"): continue
            try: d = json.loads(z.read(nm))
            except Exception: continue
            try: sid = int((d.get("sport") or {}).get("id"))
            except Exception: continue
            if sid not in RUN_SPORTS: continue
            raw = hr_series(d)
            if len(raw) < 60: continue
            hr = median_filter([min(205.0, max(40.0, v)) for v in raw])
            maxhr = ((d.get("physicalInformation") or {}).get("maximumHeartRate")) or 200
            sm, slow = ma(hr,15), ma(hr,120)
            rough = round(st.pstdev([a-b for a,b in zip(sm,slow)]),1) if len(sm)>120 else 0.0
            max5 = round(rolling_mean_max(hr,300)); dur = len(hr)/60.0
            reps = intervals(sm, REP_MAXHR_FRAC*maxhr)
            sdate = d.get("startTime","")[:19]
            # Only 2026+ sessions of >=30 min get HR-classified; everything older or shorter is
            # 'other' (the pre-2026 history has different gear/profiles the classifier isn't tuned
            # for, and short sessions aren't real training). Previously these were dropped.
            if sdate[:4] < "2026" or dur < MIN_DURATION_MIN:
                cat = "other"
            else:
                cat = classify(dur, len(reps), rough, max5)
            tp = 60 if cat == "other" else TRACE_POINTS   # coarse trace for the uncategorised history
            sessions.append(dict(
                id=(d.get("identifier") or {}).get("id","")[:8],
                date=sdate, sport=sid, cat=cat,
                dur_min=round(dur,1), hr_avg=round(st.mean(hr)), hr_max=round(max(hr)),
                max5=max5, rough=rough, nint=len(reps) if cat != "other" else 0,
                reps=[{"t":round(r["at"]/len(hr),3),"peak":r["peak"],"trough":r["trough"],"work_s":r["work_s"]} for r in reps] if cat != "other" else [],
                trace=downsample(hr, tp), trace_step_s=round(len(hr)/min(len(hr),tp)),
            ))
    sessions.sort(key=lambda s: s["date"], reverse=True)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    json.dump({"generated": int(time.time()), "count": len(sessions), "sessions": sessions},
              open(out_path,"w"), separators=(",",":"))
    by={}
    for s in sessions: by[s["cat"]]=by.get(s["cat"],0)+1
    print(f"wrote {len(sessions)} sessions -> {out_path}")
    print("  " + "  ".join(f"{k}:{v}" for k,v in sorted(by.items())))

if __name__ == "__main__":
    raw = os.environ.get("POLAR_RAW", os.path.expanduser("~/projects/private-data/polar/raw"))
    out = os.environ.get("OUT", "dist/data/training/sessions.json")
    build(raw, out)
