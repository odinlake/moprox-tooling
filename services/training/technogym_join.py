"""technogym_join.py — belt speed/incline for a session, matched by start time.

Polar knows the athlete's heart rate; the treadmill knows what it was actually asked to do. Joining
them turns "9 work reps detected" from an inference into something checkable — and on 2026-08-10 the
belt was the thing that proved the HR detector had dropped a rep.

TIMESTAMP TRAP (do not simplify this away): `startedOn` changes meaning partway through the archive.
For idCr <= 1012 it is the session END; from idCr >= 1013 it is the START. Verified against Apple
Health workout HR as an independent third clock. Using it as START throughout mis-dates the 13
oldest sessions by a full session length.
"""
import glob, json, os, re, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import errlog

CARDIO = os.path.expanduser("~/projects/private-data/technogym/cardio")
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


def load_sessions():
    """[{start, dur_s, speed, idCr}] sorted by start. Empty list if the lane is absent."""
    out = []
    for f in sorted(glob.glob(os.path.join(CARDIO, "*.json"))):
        try:
            d = json.load(open(f))
            acts = d.get("activities") or []
            if not acts:
                continue
            a = acts[0]
            dur = float(a.get("durationS") or 0)
            when = _parse_dt(d.get("startedOn"))
            if not when or dur <= 0:
                continue
            idcr = int(d.get("idCr") or 0)
            start = when - timedelta(seconds=dur) if idcr <= END_SEMANTICS_MAX_IDCR else when
            out.append({"start": start, "dur_s": dur, "idCr": idcr,
                        "speed": [(float(t), float(v)) for t, v in (a.get("speed_kph") or [])]})
        except Exception:
            continue                     # one unreadable export must not cost the whole join
    return sorted(out, key=lambda r: r["start"])


def candidates(sessions, start_iso):
    """Every belt session that could plausibly be this run, nearest start first."""
    t0 = _parse_dt(start_iso)
    if t0 is None or not sessions:
        return []
    out = []
    for s in sessions:
        if s["dur_s"] < MIN_BELT_S:
            continue                              # aborted press of Quick Start; nothing to attach
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
    def dominant_of(items):
        by = {}
        for v, h in items:
            by[v] = by.get(v, 0.0) + h
        return max(by.items(), key=lambda kv: kv[1])[0] if by else None

    # The work speed is the one the athlete spent the most TIME at among the fast band — not simply
    # the fastest segment. A single 90 s burst at 16 kph at the end of a 5x4min session at 14 would
    # otherwise become "top", push the real reps outside the work window, and the whole session would
    # degrade to a range. Time-dominance is what makes the label describe the session.
    fastest = max(v for v, _ in train)
    top = dominant_of([(v, h) for v, h in train if v >= fastest - 2.5]) or fastest

    # A "work" segment sits within 1 kph of that. 1 kph is deliberate: an opening rep at 16 against
    # 15 for the rest is the same kind of effort, not a different one.
    def is_work(v):
        return v >= top - 1.0

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
                    "idCr": belt["idCr"]}

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
            "work_s": None, "rec_s": None, "idCr": belt["idCr"]}


def choose(sessions, start_iso, trace, trace_step_s, hr_reps=None):
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
        scored.append((rep_gap, -(r if r is not None else 0.0), gap, c))
    if not scored:
        return None, ("all %d overlapping belt session(s) contradict the heart rate (%s)"
                      % (len(cands), ", ".join("idCr %s r=%+.2f" % (c["idCr"], r)
                                               for c, r in rejected if r is not None)))
    scored.sort(key=lambda x: x[:3])
    best = scored[0][3]
    if len(cands) > 1:
        return best, ("%d belt sessions overlap this run (%s); chose idCr %s"
                      % (len(cands), ", ".join(str(c["idCr"]) for c in cands), best["idCr"]))
    return best, None
