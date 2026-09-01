"""rep_clock.py — how many interval reps were actually run, read off the belt trace.

The estate has been taking rep counts from the athlete: `private-data/agents/coach/
session-structures.json` carries `planned_reps` / `completed_reps` for 5 of the 33 interval sessions
in `technogym/cardio`, and `technogym-export-lane.md` tells readers to go there for rep schemes. It
does not have to. The console runs the reps on a fixed clock, so the trace already knows.

THE LATTICE. Work-bout onsets sit on `t = ANCHOR + PERIOD*k` seconds from the activity start
(301 + 180k). Measured 2026-09-01 over all 33 cardio sessions with a bout >= 12.5 kph
(2025-12-30..2026-08-28, 187 onsets): 27/33 sessions have every onset within +-5 s of it, 110 onsets
land exactly on it, and 26 of the 27 multi-bout sessions since 2026-03-27 start their first bout in
the six-second window [301, 306] — a 5:01 warm-up held to +-5 s over 154 days, two rep schemes
(5x4' and 10x1') and target speeds 13.0-18.0 kph. 180 s is the coarsest period that fits: every rival
reaching 110 exact onsets divides 180, and nothing above 180 beats 76.

Counting occupied slots reproduces all 5 declared rep counts, including the one declared abort
(2026-07-20, idCr 1089: declared 10 planned / 9 completed, trace gives 9 slots).

WHAT THIS DOES NOT MEAN. Because the shape is preset, rep count / rep length / recovery length are
NOT evidence of decisions made during the session — same trap as `technogym-duration-is-cooldown`,
where the session duration turned out to be cool-down. What is athlete-side is the target speed
selected, how many slots were occupied, and speed reductions INSIDE an occupied slot.

PARTIAL REPS ARE A THIRD STATE. `completed_reps` is an integer and cannot say "started it and
dropped the pace". 2026-08-28 (idCr 1111) is the first instance the estate holds: 5 slots at
14.0 kph, reps 1-4 hold for all 240 s, rep 5 holds 70 s then runs 170 s at 12.0. Report it
separately from a rep that was never started.

ERA OFFSET. 2026-03-27..04-24 sessions sit consistently at residual +4/+5 s, May onward at 0/+1.
Within tolerance; do not read the 4 s shift as a change in behaviour.
"""
import json

WORK_KPH = 12.5      # separates a work bout from the 8 kph recovery and from any easy run
ANCHOR = 301         # first work bout starts here, to +-5 s, in every lattice-era session
PERIOD = 180         # 1'/2' -> every slot; 4'/2' -> every other slot
TOL_S = 5
SLOT_HOLD = 0.6      # kph below onset speed that counts as "not holding target"


def _segments(activity):
    """speed_kph is [[t, kph], ...] change points; expand to (start, end, kph) covering durationS."""
    sp, end = activity["speed_kph"], activity["durationS"]
    return [(t, sp[i + 1][0] if i + 1 < len(sp) else end, v) for i, (t, v) in enumerate(sp)]


def _speed_at(segs, t):
    for s, e, v in segs:
        if s <= t < e:
            return v
    return segs[-1][2]


def residual(t, period=PERIOD, anchor=ANCHOR):
    """Signed distance in seconds from t to the nearest lattice point."""
    r = (t - anchor) % period
    return r - period if r > period / 2 else r


def work_onsets(activity):
    """Times at which the belt crosses up into work speed."""
    out, prev = [], 0.0
    for s, _e, v in _segments(activity):
        if v >= WORK_KPH and prev < WORK_KPH:
            out.append(s)
        prev = v
    return out


def _bouts(activity):
    """Contiguous runs at or above WORK_KPH, as (start, end) in seconds."""
    out, cur = [], None
    for s, e, v in _segments(activity):
        if v >= WORK_KPH:
            cur = [s, e] if cur is None else [cur[0], e]
        elif cur is not None:
            out.append(tuple(cur))
            cur = None
    if cur is not None:
        out.append((cur[0], min(cur[1], activity["durationS"])))
    return out


def reps(activity, rep_s=None):
    """Per-rep completion for one technogym cardio activity.

    Returns {"reps": [...], "completed": n, "partial": n, "off_lattice": [...], "rep_s": n}.
    `off_lattice` holds onsets the console clock does not explain — a mid-recovery restart, a late
    rep, a manual surge. Those are the athlete-side events; do not silently drop them.

    A rep is PARTIAL when it does not hold its own onset speed for the whole nominal rep window.
    That covers both ways of easing off: dropping under work speed (the bout simply ends early, as
    on 2026-08-28) and downshifting while still running hard. `rep_s` defaults to the median bout
    length in this session — do not use the slot spacing, which is 180 s for both the 1' and the 4'
    scheme and would score every rep partial.
    """
    segs = _segments(activity)
    bouts = _bouts(activity)
    if rep_s is None:
        lens = sorted(e - s for s, e in bouts)
        rep_s = lens[len(lens) // 2] if lens else PERIOD
    # Slack absorbs belt ramp and change-point rounding. It has to be generous enough not to call
    # 2026-07-20 rep 9 partial — the operator declared that one a clean stop, and the trace has it
    # 5 s short of its 59 s siblings.
    slack = max(5, int(0.10 * rep_s))
    out, off = [], []
    for s, _e in bouts:
        if abs(residual(s)) > TOL_S:
            off.append(s)
            continue
        target = _speed_at(segs, s + 15)         # +15 s: past the belt's ramp to the set speed
        end = min(s + rep_s, activity["durationS"])
        held = sum(1 for x in range(s, end) if _speed_at(segs, x) >= target - SLOT_HOLD)
        short = (end - s) - held
        out.append({"t": s, "target_kph": target, "window_s": end - s, "held_s": held,
                    "shortfall_s": short, "partial": short > slack})
    return {"reps": out, "rep_s": rep_s,
            "completed": sum(1 for r in out if not r["partial"]),
            "partial": sum(1 for r in out if r["partial"]),
            "off_lattice": off}


def reps_for_file(path, index=0):
    with open(path) as fh:
        return reps(json.load(fh)["activities"][index])


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        r = reps_for_file(p)
        print(p, json.dumps(r))
