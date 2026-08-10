"""technogym_join.py — belt speed/incline for a session, matched by start time.

Polar knows the athlete's heart rate; the treadmill knows what it was actually asked to do. Joining
them turns "9 work reps detected" from an inference into something checkable — and on 2026-08-10 the
belt was the thing that proved the HR detector had dropped a rep.

TIMESTAMP TRAP (do not simplify this away): `startedOn` changes meaning partway through the archive.
For idCr <= 1012 it is the session END; from idCr >= 1013 it is the START. Verified against Apple
Health workout HR as an independent third clock. Using it as START throughout mis-dates the 13
oldest sessions by a full session length.
"""
import glob, json, os, re
from datetime import datetime, timedelta, timezone

CARDIO = os.path.expanduser("~/projects/private-data/technogym/cardio")
END_SEMANTICS_MAX_IDCR = 1012      # <= this: startedOn is the END of the session
MATCH_TOLERANCE_S = 420            # 7 min: clocks drift, and the watch rarely starts with the belt
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


def match(sessions, start_iso, dur_min):
    """Nearest belt session whose start is within tolerance. None if nothing lines up."""
    t0 = _parse_dt(start_iso)
    if t0 is None or not sessions:
        return None
    best, best_gap = None, None
    for s in sessions:
        gap = abs((s["start"] - t0).total_seconds())
        if gap <= MATCH_TOLERANCE_S and (best_gap is None or gap < best_gap):
            best, best_gap = s, gap
    return best


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
