"""
PART A — REVISIONS (R1–R7)
========================================================================
Patch module applying the gaps found in Part C. Import alongside
part_a_logic.py. Kept separate so the base logic reads cleanly and the
additions are auditable.

    from part_a_logic import Athlete, analyse, moving_average, ...
    from part_a_revisions import (reject_if_binned, flag_easy_session,
                                  trend_chart_spec, analyse_safe)
"""

import numpy as np
import part_a_logic as A


# ---- R1: reject pre-binned input -------------------------------------
# Classifier logic assumes ~1 Hz. If the data is pre-binned (e.g. 2-min
# rows) interval structure is destroyed and everything looks like tempo.
def reject_if_binned(timestamps_s=None, hr=None, max_spacing_s=5.0):
    """Raise if the median sample spacing exceeds max_spacing_s. Pass either
    explicit timestamps (seconds) or rely on caller guaranteeing 1 Hz.
    Returns True if OK."""
    if timestamps_s is None:
        return True  # caller asserts 1 Hz
    ts = np.asarray(timestamps_s, float)
    if len(ts) < 2:
        return True
    spacing = float(np.median(np.diff(ts)))
    if spacing > max_spacing_s:
        raise ValueError(
            f"Input appears pre-binned (median spacing {spacing:.0f}s > "
            f"{max_spacing_s:.0f}s). Interval structure cannot be recovered; "
            f"do NOT classify on this. Re-fetch per-second data.")
    return True


# ---- R2: HR-lag correction in work-bout duration ---------------------
# HR lags mechanical work by ~20–40 s; a 1-min hard rep peaks ~20–40 s into
# recovery. So the HR peak sits LATER than the true work bout. When
# estimating bout duration from HR, shift the window earlier by HR_LAG_S.
HR_LAG_S = 25  # JUDGEMENT: mid-range of the 20–40 s lag noted in-chat.

def estimate_work_bout_durations_lagcorrected(block, peaks, troughs, ath):
    block = np.asarray(block, float)
    sm = A.moving_average(block, A.SMOOTH_STRUCT_S)
    durs = []
    for p in peaks:
        before = [t for t in troughs if t < p]
        after  = [t for t in troughs if t > p]
        t0 = before[-1] if before else max(0, p - 180)
        t1 = after[0]  if after  else min(len(sm)-1, p + 180)
        midline = (sm[p] + min(sm[t0], sm[t1])) / 2.0
        l = p
        while l > t0 and sm[l] >= midline: l -= 1
        r = p
        while r < t1 and sm[r] >= midline: r += 1
        # shift the whole window earlier to undo HR lag
        width = (r - l)
        durs.append(float(width))   # width itself is lag-invariant to 1st order;
        # the SHIFT matters when aligning bouts to wall-clock, not for width.
        # We keep width as-is but expose the lag for alignment uses:
    return durs

WORKBOUT_LAG_NOTE = (
    "Bout WIDTH is ~lag-invariant; bout POSITION is shifted ~25 s late by HR "
    "lag. If you align bouts to a prescribed work/rest clock, subtract "
    f"{HR_LAG_S}s from peak positions first.")


# ---- R3: tempo plateau is drift-robust (confirm present) -------------
# Implemented in A.classify_session already (upper-quartile median). This
# wrapper exposes it standalone for reuse/testing.
def tempo_plateau(block, ath):
    sm = A.moving_average(block, A.SMOOTH_STRUCT_S)
    return float(np.median(sm[sm >= np.percentile(sm, 75)]))


# ---- R4 + R5: easy-session flagging ----------------------------------
EASY_MIN_STOP_MIN   = 20.0   # JUDGEMENT: stop earlier than this = malformed
EASY_POST_RISE_FRAC = 0.05   # HR rising >5% after stop = malformed

def flag_easy_session(block, ath):
    """Return (ok, reason). Mirrors the in-chat reject path for easy-session
    envelope fitting. Flagged sessions should be PLOTTED SEPARATELY and NOT
    fit. Most flagged ones historically were trail runs."""
    start, stop = A.find_work_block(block, ath)
    t_stop_min = stop / 60.0
    sm = A.moving_average(block, 30)
    if stop <= start:
        return False, "no work block / no stop found"
    if t_stop_min < EASY_MIN_STOP_MIN:
        return False, f"stop too early ({t_stop_min:.1f} min < {EASY_MIN_STOP_MIN:.0f})"
    after = sm[stop:]
    if len(after) > 30 and after.max() > sm[stop] * (1 + EASY_POST_RISE_FRAC):
        return False, f"HR rises >5% after stop ({after.max():.0f} > {sm[stop]:.0f})"
    return True, "ok"


# ---- R6: trend-chart specification (across sessions) ------------------
trend_chart_spec = {
    "x_axis": "dates; SNUG to data (pad ~1 day each side)",
    "month_markers": "vertical line + horizontal month label ONLY if the "
                     "month-start falls inside the plotted date range "
                     "(no orphan markers outside the axes)",
    "tick_labels": "HORIZONTAL only, never rotated",
    "series": [
        "per-session bi-exp asymptote (easy sessions)",
        "per-session 5-min rolling-average maximum",
    ],
    "shading": "fill between asymptote and 5-min-max; colour by which is "
               "higher (two distinct colours, e.g. orange when 5min-max>"
               "asymptote, green when asymptote>5min-max)",
    "trend_fit": "LINEAR with 95% CI band is the honest default. Report "
                 "slope in bpm/month.",
    "exponential_asymptote": "DECLARED NON-IDENTIFIABLE with current data: "
                 "~6 bpm scatter over ~100 days, R^2~0.07; the exp floor "
                 "slides to whatever lower bound is set. Do NOT present a "
                 "fitted 'HR is heading toward X' asymptote as real until "
                 "there is more data / less per-session noise.",
    "y_axis": "fit to data; for easy-asymptote trends ~135–165 typical",
    "exclude": "honour any manually excluded dates (see R7) before fitting",
}


# ---- R7: explicit date exclusion -------------------------------------
def filter_excluded(sessions, exclude_dates):
    """sessions: list of dicts each with a 'date' (datetime.date or str).
    exclude_dates: set of the same type. Returns the kept sessions."""
    ex = set(str(d) for d in (exclude_dates or set()))
    return [s for s in sessions if str(s.get("date")) not in ex]


# ---- safe orchestration wrapper --------------------------------------
def analyse_safe(hr, duration_min, ath, sport_label="", timestamps_s=None):
    """analyse() with the R1 guard and, for easy sessions, the R4/R5 flag."""
    reject_if_binned(timestamps_s, hr)            # R1
    res = A.analyse(hr, duration_min, ath, sport_label)
    st = res["classification"].session_type
    if st in ("easy", "trail_easy"):
        block = res["block"]
        ok, reason = flag_easy_session(block, ath)  # R4/R5
        res["easy_flag_ok"] = ok
        res["easy_flag_reason"] = reason
        if not ok:
            res["easy_fit"] = None  # do not fit flagged easy sessions
    return res


if __name__ == "__main__":
    # quick check that the patch imports & the flag path runs
    rng = np.random.default_rng(1)
    base = 140 - 60*np.exp(-np.arange(2700)/180.0)
    hr = base + rng.normal(0, 2, size=base.shape)
    ath = A.Athlete()
    res = analyse_safe(hr, 45, ath, "Treadmill running")
    print("type:", res["classification"].session_type,
          "| flag_ok:", res.get("easy_flag_ok"),
          "| reason:", res.get("easy_flag_reason"))
