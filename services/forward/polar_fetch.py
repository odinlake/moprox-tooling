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
sys.path.insert(0, str(Path.home() / "projects/moprox-tooling/services/lib"))
import errlog
from run import run_agent
import strap_health              # is the chest-strap battery going? (see its docstring)
import tg
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

# AccessLink sample_type -> a name we can read two years from now. Only 0 (HR) has ever appeared
# here, because the athlete records with a chest strap and no other sensors — but a bike with a
# power meter or cadence sensor would start filling the rest in, and a stored file that names them
# is one that needs no migration when it does. Unknown types are kept under their raw integer
# rather than dropped: capture first, understand later.
SAMPLE_NAMES = {"0": "hr", "1": "cadence", "2": "altitude", "3": "power", "4": "speed",
                "5": "distance", "6": "temperature", "7": "moving_type", "8": "rr"}

def samples_by_name(ex):
    """Every sample series the exercise carries, keyed by name. Recording rate is kept alongside,
    because a 5 s series and a 1 s series are not interchangeable and the difference is invisible
    once the numbers are in a list."""
    out = {}
    for smp in ex.get("samples") or []:
        st = str(smp.get("sample_type"))
        name = SAMPLE_NAMES.get(st, "type_%s" % st)
        vals = [None if (v == "" or v == "null") else float(v)
                for v in (smp.get("data") or "").split(",")] if smp.get("data") else []
        out.setdefault(name, []).append({"rate_s": int(smp.get("recording_rate") or 1),
                                         "n": len(vals), "data": vals})
    return out

def store_raw(ex, hr):
    """Store EVERY exercise, every sport, whole. `hr` stays a top-level key for the readers that
    already expect it; `samples` is the lossless view. Operator instruction 2026-08-27: capture
    everything now so the analysis can be revised backwards later."""
    INCOMING.mkdir(parents=True, exist_ok=True)
    fid = re.sub(r"[^A-Za-z0-9_-]", "_", str(ex.get("id") or ex.get("start_time", "ex"))[:40])
    (INCOMING / ("exercise_%s.json" % fid)).write_text(
        json.dumps({"summary": ex, "hr": hr, "samples": samples_by_name(ex),
                    "stored_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                   separators=(",", ":")))

def upload_age_h(ex):
    up = (ex.get("upload_time") or "")[:19]            # e.g. 2026-06-24T11:40:40 (Z/UTC)
    try: return (time.time() - calendar.timegm(time.strptime(up, "%Y-%m-%dT%H:%M:%S"))) / 3600.0
    except Exception: return 0.0

# What kind of session is this? Matched on the NAME, not Polar's numeric sport id: the ids for
# cycling are unverified here (no ride has ever arrived) and guessing one would silently mis-route
# the first real one. The names come straight from AccessLink's `sport` / `detailed_sport_info`.
RUN_WORDS  = ("RUN", "JOG")
RIDE_WORDS = ("CYCLING", "BIKING", "BIKE", "SPINNING", "HANDCYCLING")

def sport_kind(ex):
    """'run' | 'ride' | 'other' — 'other' is ingested too, never dropped."""
    label = ("%s %s" % (ex.get("sport") or "", ex.get("detailed_sport_info") or "")).upper()
    if any(w in label for w in RUN_WORDS):  return "run"
    if any(w in label for w in RIDE_WORDS): return "ride"
    return "other"

# What coach must be told when the session is not a run. athlete.json's LT1 155 / LT2 180 / max 202
# are RUNNING numbers; cycling HR runs roughly 5-10 bpm lower for the same relative effort, so
# applying them to a ride under-reads every intensity. We do not have cycling thresholds yet and are
# not inventing them — coach is told to report what is measurable and to name what it would need.
NON_RUN_CAVEAT = """

THIS IS NOT A RUN — sport is %s.
- athlete.json (LT1 155, LT2 180, max 202) is RUNNING physiology. Cycling HR runs ~5-10 bpm LOWER at
  the same relative effort, so those thresholds UNDER-read a ride. Do NOT apply them as zones and do
  NOT report a running session type (easy/tempo/vo2max) for this session.
- The computed classification above is the RUN classifier's output and is included only so you can
  see what it said. Treat it as unreliable for this sport.
- Report what IS measurable and sport-neutral: duration, HR range, 5-min max, drift, recovery shape,
  and comparison with this athlete's OTHER sessions of the SAME sport if any exist.
- Say plainly what you would need to read rides properly (a cycling threshold set, and power/cadence
  which Polar only receives if the bike is paired as a sensor). Do not guess it into existence.
- No cycling baseline exists yet, so early rides are a baseline being built, not a performance to
  judge. Say so rather than filling the gap with running-shaped conclusions.
"""

def post_session(ex, hr):
    """A new workout came in. Coach OWNS the post: it builds its OWN light-mode chart and sends ONE
    Telegram message — the chart with the read written into its caption (its firm standing rule; no
    separate chart, no separate text). The pipeline no longer draws a chart or sends any commentary
    of its own; it just hands coach the computed analysis + the recent conversation (so coach applies
    the latest feedback) and lets the expert do the rest.

    The ONE exception is the receipt below. Coach still owns the session post; this is an
    acknowledgement, not commentary, and must never grow into a second opinion."""
    dur_min = len(hr) / 60.0
    clean = [h for h in hr if 30 < h < 220]
    # Immediate receipt. Coach takes 3-7 min to produce its read (charting-library work can push the
    # tail out), during which a synced session otherwise sits completely silent. Plain and factual —
    # no classification, no numbers coach is about to interpret — so it reads as "received, working
    # on it" and cannot be mistaken for the read itself. Tagged #polar (operator, 2026-08-14: every
    # message carries a handle): a DIFFERENT handle from #coach serves that same separation better
    # than no handle did, since an untagged message is the one nobody can attribute at all.
    # Best-effort: a Telegram failure here must never stop the handoff to coach.
    try:
        sport = (ex.get("sport") or "session").replace("_", " ").lower()
        if clean:
            tg.send("Got %.0f min %s session, HR %d to %d. Pinging coach…"
                    % (dur_min, sport, min(clean), max(clean)), agent="polar")
        else:
            tg.send("Got %.0f min %s session. Pinging coach…" % (dur_min, sport), agent="polar")
    except Exception as e:
        print("polar: receipt failed (continuing to coach): %s" % e)
    # Strap health before the read: a dropout second is not physiology, and coach must not fit a
    # curve through it or explain it as fatigue. See services/training/strap_health.py for what the
    # trace can and cannot show (it detects the failure; it does not promise to predict it).
    strap_line = ""
    try:
        _srec, _slevel, _smsg = strap_health.record(ex.get("start_time") or "", hr)
        if _slevel != "ok":
            strap_line = ("\n\nSTRAP HEALTH (%s): %s\nSay this to Mikael IN THE POST — one clear "
                          "line, at the top if the level is 'replace', and do NOT read the affected "
                          "seconds as physiology.\n" % (_slevel.upper(), _smsg))
    except Exception as e:
        errlog.err("polar_fetch: strap health check", e)     # never blocks the session read
    res = analyse_safe(clean, dur_min, ATH, ex.get("sport") or "")
    cls = res["classification"]; m5 = res.get("five_min_max")
    fid = re.sub(r"[^A-Za-z0-9_-]", "_", str(ex.get("id") or ex.get("start_time", "ex"))[:40])
    kind = sport_kind(ex)
    summary = {"exercise_id": ex.get("id"), "raw_file": str(INCOMING / ("exercise_%s.json" % fid)),
               "sport": ex.get("sport") or "", "sport_kind": kind,
               # For a non-run the run classifier's label is not a finding, so it is reported as
               # what it is rather than promoted into the `cat` field readers trust.
               "cat": cls.session_type if kind == "run" else kind,
               "run_classifier_said": cls.session_type if kind != "run" else None,
               "date": (ex.get("start_time") or "")[:19],
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
        "from the recent conversation below.%s%s\n\nRecent conversation:\n%s"
        % (json.dumps(summary), strap_line,
           "" if kind == "run" else NON_RUN_CAVEAT % (ex.get("sport") or "unknown"),
           convo.transcript(16)))
    run_agent("coach", prompt, timeout=600)   # coach sends its own single post; nothing else is sent
    return cls.session_type if kind == "run" else kind

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
        hr = hr_from(ex); store_raw(ex, hr)
        mins = len(hr) / 60.0
        # seen is added at each TERMINAL outcome only. It used to be set next to store_raw, above,
        # which meant a workout whose post threw was recorded as handled and never retried or
        # posted again — a transient Telegram failure turned into permanent, silent loss of the
        # session. Deciding not to post is terminal; failing to post is not.
        if len(hr) < MIN_HR_SECONDS:
            print("polar: %s stored, %.0f min HR — below coach gate" % (eid, mins))
            seen.add(eid); continue
        if upload_age_h(ex) > FRESH_WINDOW_H:
            print("polar: %s stored (backfill) — not posting" % eid)
            seen.add(eid); continue
        try:
            cat = post_session(ex, hr); posted += 1
            print("polar: POSTED %s (%s, %s, %.0f min)" % (eid, ex.get("sport"), cat, mins))
            seen.add(eid)
        except Exception as e:
            # Left OUT of seen so the next run retries. Note the retry is still subject to
            # FRESH_WINDOW_H above, so a failure that outlives the window stops being postable —
            # the raw data is kept either way, and this err is now the thing that says so.
            errlog.err("polar: posting exercise %s failed — left unposted, will retry next run" % eid, e)
    save_seen(seen)
    print("polar: done; posted %d" % posted)

if __name__ == "__main__":
    main()
