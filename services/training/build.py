#!/usr/bin/env python3
"""Build public-safe training data for the dashboard from a Polar Flow export.

Two private sources, merged:
  - the bulk Polar Flow export ZIP (history), and
  - live per-exercise files dropped by the Polar fetcher (services/forward/polar_fetch.py) under
    private-data/polar/incoming as {"summary": <accesslink exercise>, "hr": [...]}.
Each running session is classified with the shared analysis library on the FULL per-second HR —
never the downsampled trace. Emits ONE public-safe JSON: per-session physiological type, HR stats,
interval structure, and a downsampled pure-HR trace (no GPS/names). Incoming sessions are deduped
against the export by date (a session that later lands in an export is not double-counted).

  POLAR_RAW   dir holding the Polar export *.zip   (default: ../private-data/polar/raw)
  POLAR_IN    dir holding live exercise_*.json      (default: ../private-data/polar/incoming)
  OUT         output JSON path                     (default: ./dist/data/training/sessions.json)
"""
import datetime, glob, json, os, sys, time, zipfile
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analysis import Athlete, analyse_safe, med10   # the validated engine
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1] / "lib"))
import errlog  # noqa: E402  — no silent swallows; see services/lib/errlog.py

RUN_SPORTS = {1, 17, 83}
ATHLETE_JSON = os.path.expanduser("~/projects/private-data/agents/coach/athlete.json")
ATH = Athlete.load(ATHLETE_JSON)   # canonical physiology the coach owns (falls back to defaults)
TRACE_POINTS = 120               # classified sessions (detail chart; the rich chart is the Telegram one)
TRACE_POINTS_THIN = 50           # unknown / very short

def _hr_val(v):
    """One HR sample -> float in (30, 220), else None. The export is not type-clean: values are
    usually floats but at least one block ships a QUOTED "NaN" string, and `30 < v < 220` on a str
    raises TypeError. That block happens to sit on a sport outside RUN_SPORTS, so the sport filter
    shields the build today — coerce here so a quoted NaN in a running session can't kill it."""
    try: f = float(v)
    except (TypeError, ValueError): return None
    return f if 30 < f < 220 else None      # NaN fails both comparisons, so it is dropped here too

def hr_series(d):
    ex = (d.get("exercises") or [{}])[0]
    best = []
    for s in (ex.get("samples") or {}).get("samples", []):
        if s.get("type") == "HEART_RATE":
            vals = [f for f in (_hr_val(v) for v in (s.get("values") or [])) if f is not None]
            if len(vals) > len(best): best = vals
    return best

def _round_or_none(v):
    return None if v is None else round(v)

def downsample(x, n):
    x = np.asarray(x, float)
    if len(x) <= n: return [round(float(v)) for v in x]
    step = len(x) / n
    return [round(float(np.mean(x[int(i*step):max(int(i*step)+1, int((i+1)*step))]))) for i in range(n)]

SPEED_OVERRIDES = os.path.expanduser("~/projects/private-data/training/speed-overrides.json")


def attach_speeds(sessions):
    """Add speeds / n-intervals to each session, and say WHERE each came from.

    Three tiers, and the dashboard renders the weak one differently on purpose:
      operator   - the athlete said so in chat (speed-overrides.json). Authoritative.
      technogym  - the belt's own record of what it was asked to do. Measured.
      est        - nothing corroborates it: the speed is the median of CORROBORATED sessions of the
                   same category, and the rep count is the HR detector's inference.

    An estimate is shown in the same column as a measurement, so it has to announce itself — see
    `spd_src` / `nint_src`, which the table renders in small italic.
    """
    try:
        import technogym_join as TG
    except Exception as exc:
        errlog.err("build.py: technogym_join unavailable — no session will carry belt speeds", exc)
        return
    belts = TG.load_sessions()
    try:
        overrides = json.load(open(SPEED_OVERRIDES)) if os.path.exists(SPEED_OVERRIDES) else {}
    except Exception as exc:
        errlog.err(f"build.py: cannot read {SPEED_OVERRIDES} — operator corrections ignored", exc)
        overrides = {}

    for s in sessions:
        s["spd"], s["spd_src"], s["nint_src"] = None, None, "est"
        # s["nint"] is still the HR-derived rep count here — attach_speeds only overwrites it once
        # a belt session has been accepted, so it is independent evidence at this point.
        b, complaint = TG.choose(belts, s.get("date", ""), s.get("trace"), s.get("trace_step_s"),
                                 hr_reps=s.get("nint"))
        if complaint:
            # Either nothing agreed with the HR, or several sessions overlapped and one was chosen.
            # Both are worth saying out loud: a shared gym is how a stranger's workout gets published
            # as yours, and it happens without anything failing.
            errlog.warn("technogym: %s run — %s" % (s.get("date", "")[:10], complaint))
        if b:
            info = TG.summarise(b)
            if info and info["speeds"]:
                s["spd"], s["spd_src"] = info["speeds"], "technogym"
                s["wk_s"], s["rc_s"] = info.get("work_s"), info.get("rec_s")
                if info["reps"]:
                    s["nint"], s["nint_src"] = info["reps"], "technogym"
        o = overrides.get(s.get("date", "")[:19]) or overrides.get(s.get("date", "")[:10])
        if o:                                    # the athlete outranks the machine
            if o.get("speeds"): s["spd"], s["spd_src"] = o["speeds"], "operator"
            if o.get("reps"):   s["nint"], s["nint_src"] = int(o["reps"]), "operator"

    # Fill the gaps from what the corroborated sessions of the same category actually ran.
    by_cat = {}
    for s in sessions:
        if s.get("spd_src") in ("technogym", "operator") and s.get("spd"):
            by_cat.setdefault(s["cat"], []).append(s["spd"])
    prior = {c: max(set(v), key=v.count) for c, v in by_cat.items()}      # most common label
    for s in sessions:
        if not s.get("spd") and prior.get(s["cat"]):
            s["spd"], s["spd_src"] = prior[s["cat"]], "est"


def make_session(hr, sport, date, sid, sess_id, source="polar"):
    """Classify one running session's per-second HR into the public-safe dashboard record, or None."""
    if len(hr) < 60: return None
    try: res = analyse_safe(hr, len(hr) / 60.0, ATH, sport)
    except Exception: return None
    cls = res["classification"]
    block = np.clip(np.asarray(res["block"], float), 40, 210)     # display-clean the trace only
    cat = cls.session_type
    m5 = res.get("five_min_max")
    max5 = round(float(m5)) if m5 == m5 else round(float(np.nanmax(block)))   # m5!=m5 => NaN
    reps = []
    if cat in ("speed", "vo2max") and len(res.get("peaks_min", [])):
        blen = max(1.0, len(block))
        troughs = list(zip(res.get("troughs_min", []), res.get("troughs_hr", [])))
        # work_s was hardcoded to 0, so every rep in the dashboard read "0s" — a whole column of
        # zeros that looked like a data outage. The engine has estimated bout durations all along;
        # they just were never carried through. NOTE this is an HR-shaped estimate (time spent in the
        # upper part of each peak-trough cycle) and runs ~100 s against the belt's 60 s target reps:
        # the two measure different things, and where the belt is present it is the better answer.
        bouts = list(res.get("bout_s") or [])
        for i, (pm, ph) in enumerate(zip(res["peaks_min"], res["peaks_hr"])):
            before = [th for tm, th in troughs if tm < pm]
            reps.append({"t": round(pm * 60.0 / blen, 3), "peak": round(float(ph)),
                         # None, not the trace minimum: "recovery" means the dip the athlete came
                         # UP from, so for the first rep there is nothing to report — falling back to
                         # min(block) reported the warm-up baseline as if it were a recovery.
                         "trough": round(float(before[-1])) if before else None,
                         "work_s": int(round(bouts[i])) if i < len(bouts) else None})
    tp = TRACE_POINTS if cat in ("easy", "tempo", "speed", "vo2max", "trail_easy") else TRACE_POINTS_THIN
    return dict(
        id=str(sess_id)[:8], date=(date or "")[:19], sport=sid, cat=cat, source=source,
        dur_min=round(len(hr) / 60.0, 1), hr_avg=round(float(np.mean(block))),
        hr_max=round(float(np.max(block))), max5=max5, med10=_round_or_none(med10(hr)),
        nint=cls.n_work_bouts,
        above_lt2=bool(cls.above_lt2), clamp=bool(cls.hr_clamp_suspected), reps=reps,
        trace=downsample(block, tp), trace_step_s=round(len(block) / min(len(block), tp)))

def from_export(raw_dir):
    zips = sorted(glob.glob(os.path.join(raw_dir, "*.zip")))
    if not zips: return []
    out = []
    with zipfile.ZipFile(zips[-1]) as z:
        for nm in z.namelist():
            if "training-session_" not in nm or not nm.endswith(".json"): continue
            try: d = json.loads(z.read(nm))
            except Exception as _e:
                errlog.skip("build.py: session json", _e)
                continue
            try: sid = int((d.get("sport") or {}).get("id"))
            except Exception as _e:
                errlog.skip("build.py: sport id", _e)
                continue
            if sid not in RUN_SPORTS: continue
            s = make_session(hr_series(d), d.get("name") or "", d.get("startTime", ""), sid,
                             (d.get("identifier") or {}).get("id", ""))
            if s: out.append(s)
    return out

def from_incoming(in_dir):
    """Live exercise_*.json from the Polar fetcher: {"summary": <accesslink exercise>, "hr": [...]}."""
    out = []
    for fp in sorted(glob.glob(os.path.join(in_dir, "exercise_*.json"))):
        try: d = json.loads(open(fp).read())
        except Exception as _e:
            errlog.skip("build.py: exercise json", _e)
            continue
        ex = d.get("summary") or {}; sport = str(ex.get("sport") or "")
        if "RUN" not in sport.upper() and "JOG" not in sport.upper(): continue   # runs only on the dash
        date = ex.get("start_time") or ex.get("start-time") or ""   # /v3/exercises vs transaction shape
        s = make_session([h for h in (d.get("hr") or []) if 30 < h < 220], sport,
                         date, 1, ex.get("id", ""))
        if s: out.append(s)
    return out

def from_apple_health(csv_path):
    """Inferred sessions from an Apple Health HR CSV (inferior devices; kept as 'unknown')."""
    if not csv_path or not os.path.exists(csv_path): return []
    try:
        import apple_health
        ss = apple_health.sessions(csv_path)
        for s in ss: s["source"] = "apple_health"        # keep provenance explicit
        return ss
    except Exception as e:
        print("apple-health: %s" % e); return []

def from_fitbit(fitbit_dir):
    """Deliberate Fitbit workouts, pre-extracted as {"summary": <exercise>, "hr": [1 Hz bpm]} by the
    Drive extractor. HR was reconstructed from the Fitbit intraday export, so these classify exactly
    like Polar runs. Running only; tagged source=fitbit. Fills the 2022-23 device gap (pre-Polar)."""
    out = []
    for fp in sorted(glob.glob(os.path.join(fitbit_dir or "", "exercise_*.json"))):
        try: d = json.loads(open(fp).read())
        except Exception as _e:
            errlog.skip("build.py: fitbit json", _e)
            continue
        ex = d.get("summary") or {}
        hr = [h for h in (d.get("hr") or []) if 30 < h < 220]
        # The Fitbit log window often runs long past the effort. Trim to the ELEVATED interval — the
        # first..last second at running HR — so duration matches real work, not the idle lead/tail.
        elev = [i for i, h in enumerate(hr) if h >= 100]
        if not elev: continue
        hr = hr[elev[0]:elev[-1] + 1]
        start = ex.get("start_time", "")
        if start and elev[0]:                            # shift start to when the effort actually began
            try: start = (datetime.datetime.fromisoformat(start) + datetime.timedelta(seconds=elev[0])).strftime("%Y-%m-%dT%H:%M:%S")
            except Exception as _e:
                errlog.skip("build.py: start shift", _e)
                pass
        s = make_session(hr, ex.get("activityName") or "Run", start, 1, ex.get("id", ""), source="fitbit")
        # Drop "forgot to stop the tracker" logs (near-resting HR): a real run averages well above 95.
        if s and s["hr_avg"] >= 95: out.append(s)
    return out

def build(raw_dir, out_path, in_dir=None, ah_csv=None, fitbit_dir=None):
    sessions = from_export(raw_dir)
    seen = {s["date"][:16] for s in sessions}        # dedup incoming vs export by minute-stamp
    for s in (from_incoming(in_dir) if in_dir else []):
        if s["date"][:16] not in seen:
            sessions.append(s); seen.add(s["date"][:16])
    # Gap-fillers only fill where a better source has NOTHING within 45 min. Priority: Polar > Fitbit
    # (real tracked workout, HR reconstructed) > Apple Health (inferred). Fitbit before AH so it wins.
    starts = [datetime.datetime.fromisoformat(s["date"]) for s in sessions]
    for s in from_fitbit(fitbit_dir) + from_apple_health(ah_csv):
        st = datetime.datetime.fromisoformat(s["date"])
        if any(abs((st - e).total_seconds()) < 2700 for e in starts): continue
        sessions.append(s); starts.append(st)
    # The classifier is only calibrated for 2026+. Everything earlier is labelled "other" until it can
    # be reviewed by hand — the physiological types (easy/tempo/…) are not trusted pre-2026.
    for s in sessions:
        if s.get("cat") == "unknown": s["cat"] = "other"     # fold the classifier's catch-all into 'other'
        if s["date"][:4].isdigit() and int(s["date"][:4]) < 2026:
            s["cat"] = "other"; s["reps"] = []
    if not sessions: sys.exit(f"no sessions from export ({raw_dir}) or incoming ({in_dir})")
    attach_speeds(sessions)
    sessions.sort(key=lambda s: s["date"], reverse=True)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    json.dump({"generated": int(time.time()), "count": len(sessions),
               "lt1": ATH.lt1_hr, "lt2": ATH.lt2_hr,     # thresholds so the dashboard can draw them
               "sessions": sessions},
              open(out_path, "w"), separators=(",", ":"))
    by = {}; src = {}
    for s in sessions:
        by[s["cat"]] = by.get(s["cat"], 0) + 1
        src[s.get("source", "polar")] = src.get(s.get("source", "polar"), 0) + 1
    print(f"wrote {len(sessions)} sessions -> {out_path}")
    print("  cat:  " + "  ".join(f"{k}:{v}" for k, v in sorted(by.items())))
    print("  src:  " + "  ".join(f"{k}:{v}" for k, v in sorted(src.items())))

if __name__ == "__main__":
    raw = os.environ.get("POLAR_RAW", os.path.expanduser("~/projects/private-data/polar/raw"))
    inc = os.environ.get("POLAR_IN", os.path.expanduser("~/projects/private-data/polar/incoming"))
    ah  = os.environ.get("APPLE_HEALTH") or next(iter(sorted(
            glob.glob(os.path.expanduser("~/projects/private-data/apple-health/*.csv")))[-1:]), None)
    fit = os.environ.get("FITBIT", os.path.expanduser("~/projects/private-data/fitbit"))
    out = os.environ.get("OUT", "dist/data/training/sessions.json")
    build(raw, out, inc, ah, fit)
