"""rep_clock.py — how many interval reps were actually run, read off the belt trace.

The estate has been taking rep counts from the athlete: `private-data/agents/coach/
session-structures.json` carries `planned_reps` / `completed_reps` for 5 of the 33 interval sessions
in `technogym/cardio`, and `technogym-export-lane.md` tells readers to go there for rep schemes. It
does not have to: the belt trace carries the reps directly.

COUNT BY DIP-MERGE, NOT BY CLOCK. A rep is a contiguous run at or above WORK_KPH, joined to the
next one when the gap between them is short (< DIP_GAP_S) and never drops below DIP_FLOOR_KPH —
a threshold re-crossing inside one rep, not a new rep. That is the whole counter. It agrees with
the earlier 301+180k lattice filter on 28 of 33 sessions and is right on all 5 where they differ
(see `rep-clock-dip-merge-beats-lattice`); the lattice had no session it alone got right.

THE RHYTHM IS PER SESSION, AND IT IS EXACT. Fit `t = anchor + period*k` to one session's own onsets
and the fit is near-perfect: over the 28 sessions with >= 3 merged onsets (178 onsets,
2025-12-30..2026-08-28), 177 sit inside +-2.0 s and exactly one onset in the whole corpus departs
from its own session's rhythm — idCr 1062, 2026-06-05, rep 5 at +77.0 s. So the fit is not a
counter, it is a DETECTOR: a residual above CLOCK_TOL_S marks the one onset that broke the rhythm
the rest of that session kept.

The period is a session constant but NOT a corpus constant, and not always a multiple of 180:
observed {180.0 x8, 300.0, 328.75, 359.5, 360.0 x13, 360.33 x3}. A fixed 301+180k lattice puts the
whole of idCr 1016 (period 328.75) 88 s out and the whole of 1024 (period 300.0) 65 s out. PERIOD /
ANCHOR below are the corpus MODES, kept only as a fallback for sessions too short to fit.

IT IS NOT A CONSOLE CLOCK — THAT READING IS WITHDRAWN. All 130 activities in `technogym/cardio`
carry a machine string ending "Quick Start" (111 "Run 9000 Excite Live: Quick Start", 19 bare):
no programmed workout ever ran, so nothing on the machine side was keeping this time. On the only
two sessions carrying an athlete-declared plan the fit returns that plan verbatim — 2026-06-29
(idCr 1076) declared "1+2 min; wu 5 min" -> period 180.00, anchor 301.0, 10 onsets; 2026-07-20
(idCr 1089) declared 10x(1'/2') aborted one short -> period 180.00, anchor 301.0, 9 onsets. The
rhythm is the athlete executing a plan by hand, and rep count, rep length and recovery length are
therefore ALL athlete decisions, as are the target speed and any speed reduction inside a rep.
Consistent with that, the residuals are late-skewed rather than symmetric: 130 of 178 are exactly
0, and of the 35 integer-second nonzero residuals inside the +-2 s band (i.e. excluding 1062's
+77 s, which is the broken rhythm and not scatter) 27 are late and 8 early, sign test p ~ 0.002.
Reaction lag against a displayed clock is a candidate for the skew, not a demonstrated cause.

PARTIAL REPS ARE A THIRD STATE. `completed_reps` is an integer and cannot say "started it and
dropped the pace". 2026-08-28 (idCr 1111) is the first instance the estate holds: 5 reps at
14.0 kph, reps 1-4 hold for all 240 s, rep 5 holds 70 s then runs 170 s at 12.0. Report it
separately from a rep that was never started.
"""
import json
import statistics

WORK_KPH = 12.5      # separates a work bout from the 8 kph recovery and from any easy run
DIP_GAP_S = 30       # gap shorter than this may be a dip inside one rep, not a rep boundary
DIP_FLOOR_KPH = 11.5 # ...but only if the belt never fell below this during the gap
ANCHOR = 301         # corpus mode of the fitted anchor; fallback only, do not count with it
PERIOD = 180         # corpus mode of the fitted period; fallback only, do not count with it
CLOCK_TOL_S = 2      # measured ceiling of per-session onset scatter; above it, the rhythm broke
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


def merged_bouts(activity):
    """Work bouts with intra-rep dips healed — one entry per rep actually run.

    Two adjacent bouts are the same rep when the gap between them is shorter than DIP_GAP_S and the
    belt never fell below DIP_FLOOR_KPH during it. That is the only structure needed to separate a
    real rep from a threshold re-crossing; no clock is consulted. idCr 1070 (2026-06-19) is the
    exhibit: 7 raw bouts, 5 reps, the two extras being 7 s and 28 s dips to 12.0 kph.
    """
    segs = _segments(activity)
    out = []
    for start, end in _bouts(activity):
        if out and start - out[-1][1] < DIP_GAP_S:
            gap = [v for s, e, v in segs if s < start and e > out[-1][1]]
            if gap and min(gap) >= DIP_FLOOR_KPH:
                out[-1] = (out[-1][0], end)
                continue
        out.append((start, end))
    return out


def fit_clock(onsets):
    """Fit this session's own `t = anchor + period*k` to its rep onsets.

    Returns {"period", "anchor", "residuals"}, or None when fewer than 3 onsets leave nothing to
    fit. Period candidates are the pairwise slopes (on[j]-on[i])/(j-i) and the anchor is a MEDIAN,
    not a mean — both so that a single late rep cannot drag the fit onto itself and hide. On idCr
    1062 a mean anchor with the median gap reports five residuals of -15 s; this reports four of
    <= 1 s and one of +77 s, which is what happened.
    """
    if len(onsets) < 3:
        return None
    best = None
    for period in sorted({(onsets[j] - onsets[i]) / (j - i)
                          for i in range(len(onsets)) for j in range(i + 1, len(onsets))}):
        if period < DIP_GAP_S:
            continue
        anchor = statistics.median([t - period * k for k, t in enumerate(onsets)])
        res = [t - (anchor + period * k) for k, t in enumerate(onsets)]
        score = (statistics.median([abs(r) for r in res]), sum(abs(r) for r in res))
        if best is None or score < best[0]:
            best = (score, {"period": period, "anchor": anchor, "residuals": res})
    return best[1]


def reps(activity, rep_s=None):
    """Per-rep completion for one technogym cardio activity.

    Returns {"reps": [...], "completed": n, "partial": n, "rep_s": n, "clock": {...} | None}.
    Every merged bout is a rep and nothing is dropped: the count is `completed + partial`.

    A rep is PARTIAL when it does not hold its own onset speed for the whole nominal rep window.
    That covers both ways of easing off: dropping under work speed (the bout simply ends early, as
    on 2026-08-28) and downshifting while still running hard. `rep_s` defaults to the median rep
    length in this session — do not use the onset spacing, which is 180 s for both the 1' and the 4'
    scheme and would score every rep partial.

    Each rep carries `residual_s` against this session's fitted rhythm and `off_clock` when that
    exceeds CLOCK_TOL_S. Off-clock is a FINDING, not a rejection — it marks a rep started well off
    the cadence the rest of the session held, e.g. an unplanned pause before it.
    """
    segs = _segments(activity)
    bouts = merged_bouts(activity)
    clock = fit_clock([s for s, _e in bouts])
    if rep_s is None:
        lens = sorted(e - s for s, e in bouts)
        rep_s = lens[len(lens) // 2] if lens else PERIOD
    # Slack absorbs belt ramp and change-point rounding. It has to be generous enough not to call
    # 2026-07-20 rep 9 partial — the operator declared that one a clean stop, and the trace has it
    # 5 s short of its 59 s siblings.
    slack = max(5, int(0.10 * rep_s))
    out = []
    for i, (s, _e) in enumerate(bouts):
        target = _speed_at(segs, s + 15)         # +15 s: past the belt's ramp to the set speed
        end = min(s + rep_s, activity["durationS"])
        held = sum(1 for x in range(s, end) if _speed_at(segs, x) >= target - SLOT_HOLD)
        short = (end - s) - held
        res = clock["residuals"][i] if clock else None
        out.append({"t": s, "target_kph": target, "window_s": end - s, "held_s": held,
                    "shortfall_s": short, "partial": short > slack, "residual_s": res,
                    "off_clock": res is not None and abs(res) > CLOCK_TOL_S})
    return {"reps": out, "rep_s": rep_s, "clock": clock,
            "completed": sum(1 for r in out if not r["partial"]),
            "partial": sum(1 for r in out if r["partial"])}


def reps_for_file(path, index=0):
    with open(path) as fh:
        return reps(json.load(fh)["activities"][index])


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        r = reps_for_file(p)
        print(p, json.dumps(r))
