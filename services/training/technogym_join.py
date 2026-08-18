"""technogym_join.py — belt speed/incline for a session, matched by start time.

Polar knows the athlete's heart rate; the treadmill knows what it was actually asked to do. Joining
them turns "9 work reps detected" from an inference into something checkable — and on 2026-08-10 the
belt was the thing that proved the HR detector had dropped a rep.

TIMESTAMP TRAP (do not simplify this away): `startedOn` changes meaning partway through the archive.
For idCr <= 1012 it is the session END; from idCr >= 1013 it is the START. Verified against Apple
Health workout HR as an independent third clock. Using it as START throughout mis-dates the 13
oldest sessions by a full session length.

SECOND TIMESTAMP TRAP: a cardio record holds ONE `startedOn` and N activities, and the activities are
neither simultaneous nor reliably sequential — 1065#1 starts 127 s BEFORE its own file's `startedOn`,
and 1094 holds two that run concurrently on different machines. Giving them all the file timestamp is
wrong on every non-first activity (median 2543 s, max 6754 s). The real per-activity times are in
`indooractivities.json`, whose `on` is the activity END; see _activity_starts().
"""
import glob, json, os, re, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import errlog

CARDIO = os.path.expanduser("~/projects/private-data/technogym/cardio")
# The per-activity clock. One record per ACTIVITY (cardio has one per SESSION), each carrying its own
# `on` = the activity END. Coverage ends 2026-07-30 while cardio runs on, so this is an OVERLAY on the
# cardio timestamps, never a replacement — anything it does not cover keeps the old anchor.
INDOOR = os.path.expanduser("~/projects/private-data/technogym/indooractivities.json")
DIST_TOL_M = 2.0                   # HDistance vs distanceM: same quantity, different rounding
# Sessions the operator has positively identified as not theirs. The account picks these up when a
# machine is left logged in, and deleting them in the Technogym app does not remove them from the
# API — verified 2026-08-10: all 102 stored sessions still return, idCr sequence unbroken. So the
# exclusion has to live here. This is a statement of fact by the operator and outranks every
# heuristic; the HR-agreement test stays as the net for ones nobody has noticed yet.
EXCLUDE = os.path.expanduser("~/projects/private-data/technogym/not-mine.json")
END_SEMANTICS_MAX_IDCR = 1012      # <= this: startedOn is the END of the session
MATCH_TOLERANCE_S = 420            # 7 min: clocks drift, and the watch rarely starts with the belt
# Start time ALONE is not enough to prove two records are the same session, but DURATION cannot be
# the test either — it disagrees for perfectly good reasons in both directions. The operator often
# leaves the belt running to watch the HR cool-down (watch longer than belt), and a flat watch
# battery truncates the other side (belt longer than watch). On 2026-02-10 the belt ran 9 min past
# the watch and is genuinely the operator's session.
#
# What does separate them is whether HR MOVES WITH the belt. Measured over 67 pairs with enough
# speed variation to test: median r = +0.60, worst genuine = -0.07, and the one foreign session
# (a stranger's progression on 2026-06-11, published against a 48 min easy run) sits alone at
# r = -0.53 — belt speed climbing while heart rate falls. The threshold sits between.
MIN_BELT_S = 300                   # under 5 min there is no usable structure, only an aborted start
MIN_SPEED_HR_R = -0.25             # reject a candidate whose HR contradicts the belt this strongly
HR_LAG_S = 25                      # HR trails a speed change by roughly this much
WARMUP_KPH = 6.0                   # at or below this we are walking/standing, not training
# The belt logs the TARGET speed at the instant it is commanded, not when it gets there — operator
# calibration 2026-08-10: roughly 1 s per kph of change, ~10 s for a typical 8->15 step, varying by
# treadmill. So a logged change point leads the athlete's actual change by that much. Durations
# survive it (both edges shift together) but TIMING does not, which matters for any alignment
# against HR. Applied in _segments().
BELT_LEAD_S_PER_KPH = 1.0
MIN_SPLIT_KPH = 2.0                # work and recovery clusters must differ by this much
SPREAD_KPH = 1.5                   # a run spanning more than this is a progression, not one pace
MIN_HOLD_S = 20.0                  # a speed must be HELD this long to be a real segment. The belt
                                   # settles over the first second or two of every rep (14.0, 13.8,
                                   # 13.3, 13.5) and ramps 0->9 in 1 s steps at the start; counting
                                   # those transients made one easy run read as "9/6.4, 3 reps" and
                                   # a 8-rep tempo read as 23.


def _parse_dt(s):
    """'2026-08-10 11:24:53 +01:00' -> aware datetime. None if unusable."""
    if not s:
        return None
    t = str(s).strip().replace("Z", "+00:00")
    t = re.sub(r"(\d{2}:\d{2}:\d{2})\.\d+", r"\1", t)     # indooractivities carries 7 fractional
                                                          # digits; %f caps at 6 and would reject it
    t = re.sub(r"([+-]\d{2}):?(\d{2})$", r"\1\2", t)      # +01:00 -> +0100
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            d = datetime.strptime(t, fmt)
            # A naive timestamp here is LOCAL, not UTC. Polar's `start_time` is local wall-clock with
            # the offset carried separately, and the belt's own timestamps are local too. Reading
            # naive as UTC put the two sides an hour apart in summer and matched nothing at all.
            return d if d.tzinfo else d.astimezone()
        except ValueError:
            continue
    return None


def _excluded():
    """Keys the operator has declared not theirs: "1065#0" for one activity, "1065" for a session."""
    try:
        with open(EXCLUDE) as f:
            return {str(k) for k in (json.load(f).get("exclude") or [])}
    except FileNotFoundError:
        return set()
    except Exception as exc:
        errlog.err(f"technogym: cannot read {EXCLUDE} — foreign sessions will NOT be excluded", exc)
        return set()


def _indoor_records():
    """[{on, dur, dist, cal}] from indooractivities.json, END-timestamped. [] if absent."""
    try:
        with open(INDOOR) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as exc:
        errlog.err(f"technogym: cannot read {INDOOR} — non-first activities keep the file timestamp",
                   exc)
        return []
    out = []
    for r in raw or []:
        pr = {p.get("n"): p.get("v") for p in (r.get("performedData") or {}).get("pr") or []}
        on = _parse_dt(r.get("on"))
        if on is None or pr.get("Duration") is None:
            continue
        out.append({"on": on, "dur": float(pr["Duration"]),
                    "dist": pr.get("HDistance"), "cal": pr.get("Calories")})
    return out


def _activity_starts(cardio_files):
    """{"1082#1": datetime} — true per-activity START, for the activities that can be paired.

    There is no shared key between the two files: `phId` is the MACHINE (2 distinct values over 113
    records), not the activity. So the pairing is on the physics both sides report — DURATION exactly,
    plus DISTANCE within rounding, with CALORIES only to break a tie. Greedy in idCr order, each
    indooractivities record consumed at most once.

    Measured 2026-08-18 over the whole archive: 124 activities, 110 pair uniquely, 3 stay ambiguous
    (1041#0, 1047#0, 1055#0 — same duration AND distance AND calories as a sibling), 11 unpaired and
    every one of those has idCr >= 1096, i.e. is past the end of indooractivities coverage. Ambiguous
    and unpaired both fall back; neither is a guess.

    `on` is the activity END, not its start. Cross-checked where the answer is independently known:
    for activity 0 with idCr >= 1013 (where `startedOn` IS the start) `on - Duration` equals it within
    1 s across all 80 such records, and for idCr <= 1012 (where `startedOn` is the END) `on` equals it
    within 1 s across all 13. Same semantics on both sides of the idCr-1012 split, so the subtraction
    is unconditional here — the split survives only in the fallback below.
    """
    recs = _indoor_records()
    if not recs:
        return {}
    used, starts = set(), {}
    for f in cardio_files:
        try:
            d = json.load(open(f))
            idcr = int(d.get("idCr") or 0)
        except Exception:
            continue                                  # load_sessions reports the read failure
        for i, a in enumerate(d.get("activities") or []):
            dur = float(a.get("durationS") or 0)
            dist = float(a.get("distanceM") or 0)
            hit = [j for j, r in enumerate(recs)
                   if j not in used and r["dur"] == dur
                   and r["dist"] is not None and abs(float(r["dist"]) - dist) <= DIST_TOL_M]
            if len(hit) > 1:
                hit = [j for j in hit if recs[j]["cal"] == float(a.get("calories") or 0)]
            if len(hit) != 1:
                continue                              # ambiguous or absent — fall back
            used.add(hit[0])
            starts[f"{idcr}#{i}"] = recs[hit[0]]["on"] - timedelta(seconds=recs[hit[0]]["dur"])
    return starts


def load_sessions():
    """One candidate per ACTIVITY, sorted by start. Empty list if the lane is absent.

    A session record can hold several activities and they are NOT one workout split in pieces —
    idCr 1065 carries three, of 35, 47.5 and 32.5 minutes, overlapping the same clock hour. Reading
    only activities[0] reported a stranger's 9->12 kph ramp as the operator's 48 min easy run, and
    separately made five real sessions look like aborted 30-second starts because a brief false start
    happened to be recorded first. 15 of 102 sessions are affected.
    """
    skip = _excluded()
    files = sorted(glob.glob(os.path.join(CARDIO, "*.json")),
                   key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0))
    starts = _activity_starts(files)
    out = []
    for f in files:
        try:
            d = json.load(open(f))
            when = _parse_dt(d.get("startedOn"))
            idcr = int(d.get("idCr") or 0)
            if not when or str(idcr) in skip:
                continue
            for i, a in enumerate(d.get("activities") or []):
                key = f"{idcr}#{i}"
                dur = float(a.get("durationS") or 0)
                if dur <= 0 or key in skip:
                    continue
                # The per-activity clock when we have it; the file's single timestamp only as the
                # fallback, and then under the idCr-1012 END/START split.
                start = starts.get(key)
                if start is None:
                    start = when - timedelta(seconds=dur) if idcr <= END_SEMANTICS_MAX_IDCR else when
                out.append({"start": start, "dur_s": dur, "idCr": idcr, "act": i, "key": key,
                            "speed": [(float(t), float(v)) for t, v in (a.get("speed_kph") or [])]})
        except Exception as exc:
            errlog.warn(f"technogym: could not read {os.path.basename(f)} ({exc})")
    return sorted(out, key=lambda r: r["start"])


def candidates(sessions, start_iso):
    """Every belt activity that could plausibly be this run, nearest start first."""
    t0 = _parse_dt(start_iso)
    if t0 is None or not sessions:
        return []
    out = []
    for s in sessions:
        if s["dur_s"] < MIN_BELT_S:
            continue                              # a false start; nothing to attach
        gap = abs((s["start"] - t0).total_seconds())
        if gap <= MATCH_TOLERANCE_S:
            out.append((gap, s))
    return [s for _, s in sorted(out, key=lambda x: x[0])]


def match(sessions, start_iso, dur_min=None):
    """Back-compatible nearest-start match. Prefer choose(), which weighs the HR evidence."""
    c = candidates(sessions, start_iso)
    return c[0] if c else None


def speed_at(belt, t):
    v = 0.0
    for ts, sp in belt["speed"]:
        if ts > t:
            break
        v = sp
    return v


def hr_agrees(belt, start_iso, trace, trace_step_s):
    """(ok, r) — does the heart rate move with the belt? r is None when it cannot be judged.

    Computed only over the OVERLAP of the two records, so a cool-down the watch kept recording and a
    session the watch stopped short of both test on the part they share. A constant-speed run
    carries no signal and is not judged.
    """
    if not trace or len(trace) < 20:
        return True, None
    try:
        import numpy as np
    except Exception:
        return True, None
    t0 = _parse_dt(start_iso)
    off = (belt["start"] - t0).total_seconds()
    step = trace_step_s or 1
    xs, ys = [], []
    for i, hr in enumerate(trace):
        tb = i * step - off - HR_LAG_S
        if 0 <= tb <= belt["dur_s"]:
            xs.append(speed_at(belt, tb)); ys.append(hr)
    if len(xs) < 20 or float(np.std(xs)) < 0.3 or float(np.std(ys)) < 0.5:
        return True, None                        # nothing varies; the test says nothing
    r = float(np.corrcoef(xs, ys)[0, 1])
    return (r >= MIN_SPEED_HR_R), r


def achieved_times(speed, dur_s):
    """[(t_reached, kph, t_left)] — change points shifted to when the belt actually got there."""
    out = []
    for i, (t, v) in enumerate(speed):
        prev = speed[i - 1][1] if i else 0.0
        reached = t + abs(v - prev) * BELT_LEAD_S_PER_KPH
        if i + 1 < len(speed):
            nt, nv = speed[i + 1]
            left = nt + abs(nv - v) * BELT_LEAD_S_PER_KPH
        else:
            left = dur_s
        out.append((reached, float(v), max(reached, left)))
    return out


def _segments(speed, dur_s):
    """[(kph, seconds_held)] over ACHIEVED time, ignoring warm-up/cool-down speeds."""
    return [(v, end - start) for start, v, end in achieved_times(speed, dur_s)
            if (end - start) >= MIN_HOLD_S]


def summarise(belt):
    """-> {'speeds': '15/8', 'reps': 10, 'work_kph': 15.0, 'rec_kph': 8.0, 'work_s': 60, 'rec_s': 120}

    The label is what the operator would say about the session, not a statistic: one number for a
    steady run, a range for a progression, work/recovery for intervals.
    """
    segs = _segments(belt["speed"], belt["dur_s"])
    train = [(v, h) for v, h in segs if v > WARMUP_KPH]
    if not train:
        return None
    # Split work from recovery by finding the natural gap in the time-weighted speed distribution
    # (a one-dimensional Otsu split), rather than taking a fixed band below the fastest segment.
    #
    # A band fails whenever a rep is not run at one speed. On 2026-05-15 each rep opened at 16-18 kph
    # and settled to 13 for the remaining three minutes; with the band anchored at 16, the 13 kph
    # bulk of every rep was classed as RECOVERY and merged with the 8 kph rest, so a 5x4+2min session
    # reported itself as "0.9+4.9". The two clusters here are unmistakable — everything at 13 and
    # above against everything at 8 — and the gap between them is what the split should find.
    thr = None
    vals = sorted({v for v, _ in train})
    if len(vals) > 1:
        best = None
        for i in range(len(vals) - 1):
            cut = (vals[i] + vals[i + 1]) / 2.0
            lo = [(v, h) for v, h in train if v < cut]
            hi = [(v, h) for v, h in train if v >= cut]
            wl, wh = sum(h for _, h in lo), sum(h for _, h in hi)
            if not wl or not wh:
                continue
            ml = sum(v * h for v, h in lo) / wl
            mh = sum(v * h for v, h in hi) / wh
            score = wl * wh * (mh - ml) ** 2          # between-class variance
            if best is None or score > best[0]:
                best = (score, cut, mh - ml)
        if best and best[2] >= MIN_SPLIT_KPH:
            thr = best[1]

    def is_work(v):
        return thr is not None and v >= thr

    # Collapse into consecutive runs of the same kind. Intervals ALTERNATE; a progression run does
    # not, and without this check one — belt ramping 9.0 -> 12.0 over 27 min — was reported as
    # "3x 11/10kph", inventing an interval session out of a steady climb. Grouping first means three
    # rising speeds in a row count as ONE effort, which is what they were.
    runs = []
    for v, h in train:
        kind = "w" if is_work(v) else "r"
        if runs and runs[-1][0] == kind:
            runs[-1][1].append((v, h))
        else:
            runs.append([kind, [(v, h)]])

    w_at = [i for i, r in enumerate(runs) if r[0] == "w"]
    reps = len(w_at)

    def dominant(items):
        by = {}
        for v, h in items:
            by[v] = by.get(v, 0.0) + h
        return max(by.items(), key=lambda kv: kv[1])[0] if by else None

    def med(vals):
        vals = sorted(vals)
        return vals[len(vals) // 2] if vals else None

    def fmt(v):
        return ("%g" % round(v, 1)) if v is not None else ""

    # Only the recoveries BETWEEN two work efforts count. The warm-up before the first rep and the
    # cool-down after the last often sit at exactly the recovery speed — 8 kph either side of 8 kph
    # recoveries — and including them pushed a 2 min recovery to a reported 3.3 min.
    if reps >= 3:
        inner = [runs[i] for i in range(w_at[0] + 1, w_at[-1]) if runs[i][0] == "r"]
        work_pairs = [p for i in w_at for p in runs[i][1]]
        rest_pairs = [p for r in inner for p in r[1]]
        if rest_pairs:
            return {"speeds": "%s/%s" % (fmt(dominant(work_pairs)), fmt(dominant(rest_pairs))),
                    "reps": reps,
                    "work_kph": dominant(work_pairs), "rec_kph": dominant(rest_pairs),
                    "work_s": int(round(med([sum(h for _, h in runs[i][1]) for i in w_at]))),
                    "rec_s": int(round(med([sum(h for _, h in r[1]) for r in inner]))),
                    "idCr": belt["idCr"], "key": belt.get("key")}

    # Not intervallic. Trim a leading or trailing segment that is SLOWER than the session's main
    # speed — that is the warm-up and the cool-down, and they are not part of what was run. Without
    # this a tempo session shaped 8 / 11 / 8 reported itself as the progression "8-11" when it was
    # simply half an hour at 11.
    body = list(train)
    main = dominant(body)
    while len(body) > 1 and body[0][0] < main:
        body.pop(0)
    while len(body) > 1 and body[-1][0] < main:
        body.pop()

    lo, hi = min(v for v, _ in body), max(v for v, _ in body)
    label = "%s\u2013%s" % (fmt(lo), fmt(hi)) if (hi - lo) > SPREAD_KPH else fmt(dominant(body))
    return {"speeds": label, "reps": 0, "work_kph": dominant(body), "rec_kph": None,
            "work_s": None, "rec_s": None, "idCr": belt["idCr"], "key": belt.get("key")}


def choose(sessions, start_iso, trace, trace_step_s, hr_reps=None, dur_min=None):
    """(belt, complaint) — the candidate that best matches this run.

    Nearest-start is NOT good enough once more than one session overlaps: a shared gym produces
    exactly that, and proximity alone let a stranger win by three seconds and publish itself as the
    operator's workout with the real one never examined.

    Nor is the speed/HR correlation enough on its own to RANK candidates, though it is a good filter.
    A smooth progression scores r=+0.60 against an interval session's own heart rate while the true
    interval belt scores +0.36 — heart rate lags and never falls as sharply as the belt does, so
    smooth curves flatter themselves. Ranking on r alone therefore prefers the wrong session.

    The decisive evidence is the REP COUNT, which the HR side derives independently of any belt: ten
    peaks in the trace against ten work bouts on the belt is agreement that a stranger's session
    cannot fake. Correlation stays as the filter, rep agreement does the ranking, and start proximity
    only breaks ties.
    """
    cands = candidates(sessions, start_iso)
    if not cands:
        return None, None
    t0 = _parse_dt(start_iso)
    scored, rejected = [], []
    for c in cands:
        ok, r = hr_agrees(c, start_iso, trace, trace_step_s)
        if not ok:
            rejected.append((c, r)); continue
        info = summarise(c) or {}
        rep_gap = abs((info.get("reps") or 0) - (hr_reps or 0)) if hr_reps is not None else 0
        gap = abs((c["start"] - t0).total_seconds())
        # Duration RANKS but never rejects — the distinction the operator drew. Activities of one
        # session all start at the same instant, so proximity cannot separate them and length is the
        # only evidence left; correlation alone chose a 25 min activity over the 48 min one that
        # actually matched a 48 min run.
        dur_gap = abs(c["dur_s"] / 60.0 - float(dur_min)) if dur_min else 0.0
        # Duration first: both records cover the same clock period, so a length that matches is the
        # strongest evidence available and the least noisy. Rep agreement then separates candidates
        # of similar length — including ones of IDENTICAL length, where it is all that is left.
        scored.append((round(dur_gap, 1), rep_gap, -(r if r is not None else 0.0), gap, c))
    if not scored:
        return None, ("all %d overlapping belt session(s) contradict the heart rate (%s)"
                      % (len(cands), ", ".join("%s r=%+.2f" % (c.get("key") or c["idCr"], r)
                                               for c, r in rejected if r is not None)))
    scored.sort(key=lambda x: x[:4])
    best = scored[0][4]
    if len(cands) > 1:
        return best, ("%d belt activities overlap this run (%s); chose %s"
                      % (len(cands), ", ".join(str(c.get("key") or c["idCr"]) for c in cands),
                         best.get("key") or best["idCr"]))
    return best, None
