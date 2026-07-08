"""
PART A — EXECUTABLE LOGIC
========================================================================
Self-contained analysis logic for treadmill/trail running HR sessions.

CONTEXT FOR A FRESH AGENT
-------------------------
You are analysing single running sessions for one specific athlete (see
training-context.md, Part B). Each session gives you:
    - a per-second heart-rate series (list/array of bpm, 1 Hz)
    - total duration (minutes)
    - max HR and resting HR (constants for this athlete)
    - a sport/type label from the watch (e.g. "Treadmill running",
      "Trail running") — coarse, NOT the physiological session type
There is NO GPS / pace / distance. Ignore location entirely.

DATA PROVENANCE (affects robustness choices)
    - Historical data: per-second, Apple-Health-filtered, hand-collected.
      Each row historically carried Min/Max/Avg over the sample window;
      treat only Avg (the instantaneous-ish value) as canonical and never
      depend on Min/Max being present.
    - Live data: Polar feed, cleaner, possibly richer per-sample. May
      contain Min/Max/Avg too.
    => PREFER logic that degrades gracefully to a plain per-second HR
       vector. Every function below takes `hr` (1 Hz bpm array) as the
       only required signal; Min/Max bands are optional decoration.

ATHLETE CONSTANTS (defaults; override per athlete)
    MAX_HR      = 202   # observed achieved max; plausible ceiling 200-205
    RESTING_HR  = 45
    LT1_HR      ~ 155   # first threshold (aerobic), estimate
    LT2_HR      ~ 180   # second threshold (anaerobic), strong estimate;
                        # corroborated by 4 independent signals + felt
                        # "resistance" at 179-180. Treat as ~180 +/- 3.

All thresholds below are EXPLICIT. Where a value encodes a judgement call
rather than a derived formula, it is tagged  # JUDGEMENT  with rationale.
"""

import numpy as np
from dataclasses import dataclass, field
from typing import Optional


# ======================================================================
# ATHLETE PROFILE (parameters, not hard-coded into functions)
# ======================================================================

@dataclass
class Athlete:
    max_hr: float = 202.0
    resting_hr: float = 45.0
    lt1_hr: float = 155.0          # JUDGEMENT: aerobic threshold estimate
    lt2_hr: float = 180.0          # JUDGEMENT: anaerobic threshold; ~180 +/- 3
    # Karvonen zone edges as fraction of HR reserve (HRR = max - rest).
    # HRR-based ("Karvonen") was explicitly chosen over %max in this chat.
    # Zone fractions are the standard 5-zone Karvonen cut points. JUDGEMENT
    # on exact edges; tune per athlete.
    @classmethod
    def load(cls, path=None):
        """Load the canonical physiology from a JSON file (the single source of truth the coach
        owns and edits), falling back to defaults. So a threshold change in one file re-tunes the
        whole classifier. Reads ATHLETE_JSON from the env if no path is given."""
        import json, os
        p = path or os.environ.get("ATHLETE_JSON")
        if p and os.path.exists(p):
            try:
                d = json.load(open(p))
                return cls(**{k: float(d[k]) for k in ("max_hr", "resting_hr", "lt1_hr", "lt2_hr")
                              if k in d})
            except Exception:
                pass
        return cls()
    def hrr(self) -> float:
        return self.max_hr - self.resting_hr
    def karvonen(self, frac: float) -> float:
        return self.resting_hr + frac * self.hrr()
    @property
    def z2_low(self):  return self.karvonen(0.60)   # ~138
    @property
    def z2_high(self): return self.karvonen(0.70)   # ~154
    @property
    def z4_low(self):  return self.karvonen(0.80)   # ~169
    @property
    def z5_low(self):  return self.karvonen(0.90)   # ~185


# ======================================================================
# SHARED SIGNAL UTILITIES
# ======================================================================

def moving_average(x: np.ndarray, window_s: int) -> np.ndarray:
    """Centered moving average over `window_s` samples (data is 1 Hz, so
    window_s == seconds). Edge-padded so output length == input length.
    Used everywhere we need a 'smoothed HR'. Default smoothing windows are
    given at each call site, not here."""
    if window_s <= 1:
        return np.asarray(x, dtype=float)
    k = int(window_s)
    kernel = np.ones(k) / k
    return np.convolve(np.asarray(x, dtype=float), kernel, mode="same")


def rolling_max(x: np.ndarray, window_s: int) -> np.ndarray:
    s = pd_rolling(x, window_s, np.max)
    return s

def rolling_mean(x: np.ndarray, window_s: int) -> np.ndarray:
    return pd_rolling(x, window_s, np.mean)

def pd_rolling(x, window_s, fn):
    """Minimal dependency-free rolling reducer, min_periods = window//2."""
    x = np.asarray(x, dtype=float)
    n = len(x); w = int(window_s); out = np.full(n, np.nan)
    minp = max(1, w // 2)
    for i in range(n):
        lo = max(0, i - w + 1)
        seg = x[lo:i+1]
        if len(seg) >= minp:
            out[i] = fn(seg)
    return out


# ======================================================================
# A.1  SESSION-TYPE CLASSIFIER
# ======================================================================
#
# TAXONOMY (developed in-chat). Physiological type, NOT the watch label:
#   "easy"      - steady aerobic, 60s-smoothed HR never exceeds EASY_CEIL
#   "tempo"     - sustained continuous effort sitting just BELOW LT2
#                 ("Tempo-minus"); plateau a few bpm under LT2, never
#                 fatiguing. Continuous, not intervallic.
#   "speed"     - short hard reps (~1 min work / ~2 min recovery). Peaks
#                 climb toward/above LT2 into VO2max region. Intervallic.
#   "vo2max"    - longer hard reps (~4 min work / ~2 min recovery). Peaks
#                 driven high (high-180s+). Intervallic.
#   "trail_easy"- easy effort on trail; slightly higher HR than treadmill
#                 easy for same RPE, shorter duration (~6 km). Still easy
#                 (below LT1). Distinguished only via the sport label +
#                 lower duration; physiologically an "easy".
#   "unknown"   - does not match; hand to a human / flag.
#
# IMPORTANT CORRECTIONS BAKED IN (we got these wrong once and fixed them):
#   * Do NOT classify on coarse (2-min-binned) HR — interval structure
#     hides and an interval session looks like a tempo. Always classify on
#     the full per-second series.
#   * "Hard" != "vo2max". A session can feel maximal yet be a clamped
#     THRESHOLD effort. Intensity *intent* (clamping HR to a target by
#     lowering speed) cannot be read from HR alone; see EXECUTION-vs-PLAN
#     note and the `hr_clamp_suspected` flag.
#   * HR ceiling being lower than usual is ambiguous: adaptation OR
#     under-fuelling OR deliberate pacing. The classifier must NOT assert a
#     cause. It only labels structure/intensity; cause is commentary (Part B)
#     and must stay hedged.
#
# ALGORITHM (generalized from the judgement calls in-chat):
#   1. Trim to the "active block": drop leading/trailing samples below an
#      activity floor; this removes pre/post standing-around. For sessions
#      that include explicit warmup/cooldown (Mon/Wed/Fri per plan) this
#      also approximately removes them, but we do NOT assume their presence.
#   2. Compute 60s-smoothed HR. If its max < EASY_CEIL -> "easy"
#      (or "trail_easy" if sport label is trail). This is the chat's
#      explicit easy-session rule.
#   3. Otherwise decide intervallic vs continuous via peak/trough
#      detection (A.2). If >= MIN_REPS distinct work bouts -> intervallic.
#   4. If intervallic: split speed vs vo2max by median work-bout DURATION
#      (short ~<=120s -> speed; long ~>=180s -> vo2max). Fallback to
#      cycle period if bout edges are noisy.
#   5. If continuous and not easy: "tempo" if smoothed plateau sits within
#      TEMPO_BAND of LT2 (at/just-below). If plateau pushes clearly above
#      LT2 for a sustained continuous block, it's a threshold/cruise effort
#      -> still report "tempo" but set `above_lt2=True` (we did not develop
#      a separate continuous-VO2max label; continuous efforts in this
#      athlete's plan are sub-LT2 tempo).

# ---- explicit thresholds for the classifier ----
EASY_CEIL_BPM          = 160.0   # JUDGEMENT (chat rule): 60s-smoothed HR
                                 # never exceeds 160 => easy session.
ACTIVITY_FLOOR_FRAC    = 0.50    # active if HR > rest + 0.50*HRR ... see fn
MIN_REPS               = 3       # >=3 work bouts => treat as intervallic
MIN_RECOVERY_BPM       = 15      # JUDGEMENT: real intervals recover deeply between reps; require the
                                 # median peak->trough swing >= this or the "bouts" are just ripples on
                                 # one continuous effort, not intervals. Empirically: genuine speed/
                                 # vo2max sit at 20-24 bpm; a continuous progression misread as vo2max
                                 # was 12 (07-08). 15 separates them with margin either side.
SHORT_BOUT_MAX_S       = 120     # work bout <=120s => "speed" candidate
LONG_BOUT_MIN_S        = 180     # work bout >=180s => "vo2max" candidate
TEMPO_BAND_BPM         = 6.0     # plateau within +/-6 bpm of LT2 => tempo
SMOOTH_EASY_S          = 60      # smoothing window for the easy test
SMOOTH_STRUCT_S        = 15      # smoothing window for peak/trough work


@dataclass
class Classification:
    session_type: str
    intervallic: bool
    n_work_bouts: int
    median_bout_s: Optional[float]
    smoothed_peak_hr: float
    above_lt2: bool
    hr_clamp_suspected: bool
    notes: list = field(default_factory=list)


def active_block_indices(hr, ath: Athlete, floor_frac=ACTIVITY_FLOOR_FRAC):
    """Indices of the contiguous active block. Active = HR above
    rest + floor_frac*HRR. We take the span from first to last active
    sample (keeps brief dips inside the block)."""
    floor = ath.resting_hr + floor_frac * ath.hrr()
    active = np.where(np.asarray(hr) > floor)[0]
    if len(active) == 0:
        return 0, len(hr) - 1
    return int(active[0]), int(active[-1])


def classify_session(hr, duration_min, ath: Athlete, sport_label: str = "") -> Classification:
    hr = np.asarray(hr, dtype=float)
    notes = []

    i0, i1 = active_block_indices(hr, ath)
    block = hr[i0:i1+1]

    sm60 = moving_average(block, SMOOTH_EASY_S)
    smoothed_peak = float(np.nanmax(sm60)) if len(sm60) else float("nan")

    is_trail = "trail" in sport_label.lower()

    # --- easy test (chat's explicit rule) ---
    if smoothed_peak < EASY_CEIL_BPM:
        stype = "trail_easy" if is_trail else "easy"
        return Classification(stype, False, 0, None, smoothed_peak,
                              above_lt2=False, hr_clamp_suspected=False,
                              notes=notes + ["60s-smoothed HR < %.0f => easy" % EASY_CEIL_BPM])

    # --- intervallic vs continuous ---
    peaks, troughs = detect_peaks_troughs(block, ath)
    n_bouts = len(peaks)
    # Count as intervals ONLY if the reps actually recover: the median peak->trough swing must clear
    # MIN_RECOVERY_BPM. Otherwise the detected "bouts" are small ripples riding one continuous climb
    # (which the prominence filter alone can't tell from real reps) — treat as continuous, not intervals.
    recovery = float(np.median(block[peaks]) - np.median(block[troughs])) if (n_bouts and len(troughs)) else 0.0
    intervallic = n_bouts >= MIN_REPS and recovery >= MIN_RECOVERY_BPM
    if n_bouts >= MIN_REPS and recovery < MIN_RECOVERY_BPM:
        notes.append("%d peaks but shallow recovery (%.0f<%d bpm) => continuous, not intervals"
                     % (n_bouts, recovery, MIN_RECOVERY_BPM))

    if intervallic:
        bout_durs = estimate_work_bout_durations(block, peaks, troughs, ath)
        med = float(np.median(bout_durs)) if len(bout_durs) else None
        if med is not None and med <= SHORT_BOUT_MAX_S:
            stype = "speed"
        elif med is not None and med >= LONG_BOUT_MIN_S:
            stype = "vo2max"
        else:
            # ambiguous bout length: fall back on peak height.
            # speed peaks tend to sit lower (climbing toward LT2/low VO2),
            # vo2max peaks driven into high-180s+. JUDGEMENT fallback.
            peak_hr_med = float(np.median(block[peaks]))
            stype = "vo2max" if peak_hr_med >= ath.lt2_hr + 5 else "speed"
            notes.append("ambiguous bout length (%.0fs); split on peak height" % (med or -1))

        # hr clamp / pacing suspicion: peaks flat & low relative to history
        # (cannot be proven from one session). Flag if the peak ENVELOPE is
        # essentially flat AND tops out below the athlete's usual VO2max
        # ceiling. This is advisory only.
        clamp = peaks_flat_and_capped(block, peaks, ath)
        above = float(np.median(block[peaks])) > ath.lt2_hr
        return Classification(stype, True, n_bouts, med, smoothed_peak,
                              above_lt2=above, hr_clamp_suspected=clamp,
                              notes=notes)

    # --- continuous, not easy => tempo / threshold ---
    # plateau = median of upper-quartile of the smoothed block (the
    # sustained level, robust to the rise-in).
    sm15 = moving_average(block, SMOOTH_STRUCT_S)
    plateau = float(np.median(sm15[sm15 >= np.percentile(sm15, 75)]))
    near_lt2 = abs(plateau - ath.lt2_hr) <= TEMPO_BAND_BPM
    above = plateau > ath.lt2_hr
    if near_lt2 or plateau < ath.lt2_hr:
        notes.append("continuous plateau %.0f bpm vs LT2 %.0f => tempo" % (plateau, ath.lt2_hr))
        return Classification("tempo", False, 0, None, smoothed_peak,
                              above_lt2=above, hr_clamp_suspected=False, notes=notes)
    # sustained clearly above LT2 (rare in this athlete's plan)
    notes.append("continuous plateau %.0f ABOVE LT2; report tempo+above_lt2" % plateau)
    return Classification("tempo", False, 0, None, smoothed_peak,
                          above_lt2=True, hr_clamp_suspected=False, notes=notes)


def peaks_flat_and_capped(block, peaks, ath: Athlete, flat_bpm=6.0):
    """Advisory clamp/pacing detector. True if the work-bout peaks span a
    narrow range (<= flat_bpm across reps 2..n) AND top out below the
    VO2max region (< LT2 + 8). Cannot distinguish adaptation vs fuelling
    vs deliberate pacing — caller must keep cause hedged."""
    if len(peaks) < 3:
        return False
    pv = block[peaks]
    span = float(np.max(pv[1:]) - np.min(pv[1:]))   # ignore rep 1 (ramp-in)
    capped = float(np.max(pv)) < (ath.lt2_hr + 8)
    return span <= flat_bpm and capped


# ======================================================================
# A.2  PEAK / TROUGH / INTERVAL DETECTION
# ======================================================================
#
# GOAL: from a per-second HR block, return work-bout PEAKS and recovery
# TROUGHS, robustly, INCLUDING the first and last reps (we mishandled the
# first rep once: a height floor of 165 + over-large min-distance dropped
# the opening low rep. Fix: low/no height floor, prominence-driven, modest
# min-distance, and search the whole work block).
#
# Approach (no scipy dependency required, but scipy used if available):
#   1. Smooth with SMOOTH_STRUCT_S (=15s) — enough to kill beat noise,
#      little enough to keep ~1 min reps.
#   2. Find the work block start/end: the first sustained rise above LT1
#      and the last fall back below it. This excludes warmup ramp and
#      cooldown so the envelope fits don't ingest the descent. CRITICAL:
#      when finding the stop, BACKTRACK to the local peak where the final
#      descent BEGAN, then exclude everything after (we forgot to backtrack
#      once and included cooldown minutes).
#   3. Peaks: local maxima, min separation MIN_CYCLE_S, prominence
#      PROMINENCE_BPM, NO height floor (so low first reps are kept).
#   4. Troughs: local minima of the same series between peaks, same
#      separation/prominence, restricted to within the work block and
#      above a sane recovery floor.
#
# DEGRADES GRACEFULLY: needs only the HR vector.

MIN_CYCLE_S     = 130    # JUDGEMENT: min seconds between successive peaks.
                         # ~2 min covers 1+2 (speed, 3-min cycle) and
                         # 4+2 (vo2max, 6-min cycle). Lowering risks double
                         # detection on noisy plateaus.
PROMINENCE_BPM  = 5.0    # JUDGEMENT: a real rep stands >=5 bpm above the
                         # surrounding trough on smoothed HR.
TROUGH_FLOOR_FRAC = None # set from LT1 at runtime (recovery rarely drops
                         # far below LT1 in dense intervals)


def _find_peaks_simple(x, distance, prominence):
    """Dependency-free peak finder: local maxima with min `distance`
    samples apart and `prominence` above the lower of the two adjacent
    valleys. Greedy by height. Adequate for smoothed HR."""
    x = np.asarray(x, float)
    cand = [i for i in range(1, len(x)-1) if x[i] >= x[i-1] and x[i] >= x[i+1]]
    cand.sort(key=lambda i: -x[i])
    chosen = []
    for i in cand:
        if all(abs(i - j) >= distance for j in chosen):
            # prominence check vs nearest valley within +/- distance
            lo = max(0, i - distance); hi = min(len(x), i + distance)
            local_min = min(x[lo:i].min() if i > lo else x[i],
                            x[i+1:hi].min() if hi > i+1 else x[i])
            if x[i] - local_min >= prominence:
                chosen.append(i)
    return sorted(chosen)


def find_work_block(block, ath: Athlete):
    """Return (start_idx, stop_idx) of the work portion. Stop = local peak
    where the final sustained descent begins (BACKTRACKED), so cooldown is
    excluded. Uses 30s smoothing for the descent logic."""
    sm = moving_average(block, 30)
    lt1 = ath.lt1_hr
    above = np.where(sm > lt1)[0]
    if len(above) == 0:
        return 0, len(block) - 1
    start = int(above[0])

    # find final descent: from the global smoothed peak, walk forward to the
    # first point that falls and STAYS below 90% of peak for >=10s, then
    # backtrack to the local max where the descent began.
    pk = int(np.argmax(sm))
    thr = sm[pk] * 0.90               # JUDGEMENT: 10% drop = "session ended"
    drop = None
    for i in range(pk, len(sm)):
        if sm[i] < thr and (i + 10 >= len(sm) or sm[i:i+10].mean() < thr):
            drop = i; break
    if drop is None:
        return start, len(block) - 1
    stop = drop
    for j in range(drop, max(pk, drop - 60), -1):   # backtrack up to 60s
        if sm[j] >= sm[stop]:
            stop = j
    return start, stop


def detect_peaks_troughs(block, ath: Athlete):
    """Return (peak_idx, trough_idx) within the work block, both as indices
    into `block`. Robust to low first reps."""
    block = np.asarray(block, float)
    start, stop = find_work_block(block, ath)
    sm = moving_average(block, SMOOTH_STRUCT_S)

    seg = sm[start:stop+1]
    if len(seg) < MIN_CYCLE_S:
        return np.array([], int), np.array([], int)

    try:
        from scipy.signal import find_peaks
        pk, _ = find_peaks(seg, distance=MIN_CYCLE_S, prominence=PROMINENCE_BPM)
        tr, _ = find_peaks(-seg, distance=MIN_CYCLE_S, prominence=PROMINENCE_BPM)
    except Exception:
        pk = np.array(_find_peaks_simple(seg, MIN_CYCLE_S, PROMINENCE_BPM))
        tr = np.array(_find_peaks_simple(-seg, MIN_CYCLE_S, PROMINENCE_BPM))

    # recovery floor: troughs shouldn't be counted if they fall to near rest
    floor = ath.lt1_hr - 25     # JUDGEMENT: keep troughs above this
    tr = np.array([i for i in tr if seg[i] > floor], int)

    return pk + start, tr + start


def estimate_work_bout_durations(block, peaks, troughs, ath: Athlete):
    """Approximate each work bout's duration as the time the smoothed HR
    stays within the upper part of each peak-trough cycle. Generalized
    from: 'work bout' ~ span around a peak above the midline between that
    peak and its neighbouring troughs. Returns list of seconds.
    Degrades to cycle-period/2 if troughs are missing."""
    block = np.asarray(block, float)
    sm = moving_average(block, SMOOTH_STRUCT_S)
    durs = []
    for p in peaks:
        # nearest trough before and after
        before = [t for t in troughs if t < p]
        after  = [t for t in troughs if t > p]
        t0 = before[-1] if before else max(0, p - 180)
        t1 = after[0]  if after  else min(len(sm)-1, p + 180)
        midline = (sm[p] + min(sm[t0], sm[t1])) / 2.0
        # width of the contiguous run around p that stays above midline
        l = p
        while l > t0 and sm[l] >= midline: l -= 1
        r = p
        while r < t1 and sm[r] >= midline: r += 1
        durs.append(float(r - l))   # samples == seconds at 1 Hz
    return durs


# ======================================================================
# A.3  PER-SESSION-TYPE CHARTING + ANNOTATION LOGIC
# ======================================================================
#
# GLOBAL CHART CONVENTIONS (locked by athlete preference + chat history):
#   * NEVER use angled/rotated tick labels anywhere. Horizontal only;
#     abbreviate or drop ticks instead. (Hard preference.)
#   * X axis = minutes from session start.
#   * For cross-session COMPARISON charts of session curves, fix x-axis to
#     0..50 min so sessions align. For single-session charts, 0..50 is the
#     default but may snug to data.
#   * For trend charts over dates, fit the x-axis SNUG to the data (±~1 day)
#     and only draw month markers that fall INSIDE the plotted range.
#   * Easy-session y-axis for the envelope/trend work: 110..160 bpm.
#   * Interval sessions y-axis: ~100..200 bpm (must show peaks+troughs).
#   * Min/Max band (if present) drawn as light shaded fill behind Avg HR.
#     Optional — absent in plain per-second data.
#   * Plot area should be reasonably tall; 2-column small-multiples when
#     showing many sessions, "very tall is fine".
#
# MODELS USED:
#   * Easy / continuous rise:  bi-exponential approach to an asymptote
#       HR(t) = HR_inf - A1*exp(-t/tau1) - A2*exp(-t/tau2)
#     Fit ONLY over [work_start .. stop] (first 30 min era historically;
#     now ~45 min as easy runs lengthened — fit to the detected work block,
#     not a fixed 30). Plot the model across the WHOLE curve but fit only
#     the identified interval.
#   * Interval peaks/troughs: each envelope is an exponential approach
#       env(t) = a - B*exp(-t/tau)
#     Fit peaks and troughs separately. Report asymptote `a` +/- std error.
#     CAVEAT to surface in annotation: if B or tau hit fit bounds, or peaks
#     are near-flat, the asymptote is NOT a reliable extrapolated max —
#     label it "settled ~X" not "-> X".

import importlib

def _curve_fit():
    try:
        return importlib.import_module("scipy.optimize").curve_fit
    except Exception:
        return None

def bi_exp(t, hr_inf, A1, tau1, A2, tau2):
    return hr_inf - A1*np.exp(-t/tau1) - A2*np.exp(-t/tau2)

def exp_env(t, a, B, tau):
    return a - B*np.exp(-t/tau)


# Per-type plotting specification. A fresh agent should read this dict and
# render accordingly with its own plotting lib (matplotlib assumed).
CHART_SPEC = {
    "easy": {
        "series": ["avg_hr", "minmax_band_if_present", "biexp_model"],
        "y_range": (110, 160),
        "x_range": (0, 50),
        "fit": "bi_exp over detected work block",
        "annotate": [
            "asymptote (HR_inf) as horizontal dotted line + value",
            "stop/end-of-work vertical line",
            "5-min rolling-average maximum (single value in title)",
            "tau1/tau2 in title (fast & slow rise constants)",
        ],
        "title": "{date} {weekday} - Easy | asymptote {Hinf:.0f} bpm | 5min-max {m5:.0f}",
    },
    "trail_easy": {  # same as easy but note trail + shorter
        "inherits": "easy",
        "title": "{date} {weekday} - Trail easy | asymptote {Hinf:.0f} | 5min-max {m5:.0f}",
        "extra_note": "expect HR a few bpm higher than treadmill easy at same RPE",
    },
    "tempo": {
        "series": ["avg_hr", "minmax_band_if_present"],
        "y_range": (110, 190),
        "x_range": (0, 50),
        "fit": "none required; optionally linear drift slope over the "
               "continuous work block (bpm/min)",
        "annotate": [
            "LT2 reference band (LT2 +/- a few bpm) shaded",
            "plateau HR (upper-quartile median) value",
            "drift slope bpm/min if computed",
            "5-min rolling-average maximum",
        ],
        "title": "{date} {weekday} - Tempo | plateau {plateau:.0f} | drift {drift:+.2f}/min | 5min-max {m5:.0f}",
    },
    "speed": {
        "series": ["avg_hr", "minmax_band_if_present",
                   "peak_markers", "trough_markers",
                   "peak_envelope", "trough_envelope"],
        "y_range": (100, 200),
        "x_range": (0, 50),
        "fit": "exp_env on peaks AND troughs separately; also amplitude "
               "decay (peak-trough per rep) as exp approach to a floor",
        "annotate": [
            "peak envelope asymptote +/- stderr (label 'settled ~X' if "
            "bounds hit / peaks flat)",
            "trough envelope asymptote +/- stderr",
            "per-rep amplitude table (peak/trough/amp) in text out",
            "amplitude-decay tau (how fast on/off swing collapses)",
            "vertical markers at each detected peak (optional)",
        ],
        "title": "{date} {weekday} - Speed ({n} reps) | peaks~{pk:.0f} troughs~{tr:.0f} | amp floor {ampf:.0f} (tau {ampt:.0f}m)",
    },
    "vo2max": {
        "inherits": "speed",
        "title": "{date} {weekday} - VO2max ({n} reps) | peaks~{pk:.0f} troughs~{tr:.0f} | 5min-max {m5:.0f} peak {peak:.0f}",
        "extra_annotate": [
            "if peak envelope params hit bounds OR median peak < LT2+8: "
            "add caption that this may be a clamped/under-target session, "
            "cause UNCERTAIN (adaptation vs fuelling vs pacing).",
        ],
    },
}


def fit_easy(block_min, block_hr, work_slice):
    """Fit bi-exp over the work slice; return params + model over full t."""
    cf = _curve_fit()
    t = block_min
    s = work_slice
    if cf is None or s.stop - s.start < 15:
        return None
    tf, hf = t[s], block_hr[s]
    hinf0 = np.percentile(hf[-30:], 75)
    Atot = hinf0 - hf[0]
    p0 = [hinf0, Atot*0.7, 3, Atot*0.3, 15]
    bounds = ([100,0,0.5,0,1],[170,80,10,80,60])
    try:
        popt,_ = cf(bi_exp, tf, hf, p0=p0, bounds=bounds, maxfev=15000)
    except Exception:
        return None
    return dict(params=popt, asymptote=float(popt[0]),
                model=bi_exp(t, *popt))


def fit_envelope(times_min, values):
    cf = _curve_fit()
    if cf is None or len(times_min) < 3:
        return None
    p0 = [max(values)+5, 40, 15]
    bounds = ([min(values)-5, 5, 2], [220, 150, 80])
    try:
        popt, pcov = cf(exp_env, times_min, values, p0=p0, bounds=bounds, maxfev=20000)
    except Exception:
        return None
    err = np.sqrt(np.diag(pcov))
    # reliability flag: bounds hit or near-flat
    bounds_hit = (abs(popt[1]-bounds[1][1]) < 1e-3 or abs(popt[2]-bounds[1][2]) < 1e-3
                  or abs(popt[2]-bounds[0][2]) < 1e-3)
    return dict(asymptote=float(popt[0]), stderr=float(err[0]),
                tau=float(popt[2]), reliable=not bounds_hit)


def five_min_max(hr):
    return float(np.nanmax(rolling_mean(hr, 300)))


# ======================================================================
# ORCHESTRATION
# ======================================================================

def analyse(hr, duration_min, ath: Athlete, sport_label=""):
    """Top-level: classify, detect structure, and produce the plot spec +
    computed stats a renderer needs. Returns a dict; does not draw."""
    hr = np.asarray(hr, float)
    i0, i1 = active_block_indices(hr, ath)
    block = hr[i0:i1+1]
    t_min = np.arange(len(block)) / 60.0

    cls = classify_session(hr, duration_min, ath, sport_label)
    out = dict(classification=cls, t_min=t_min, block=block,
               five_min_max=five_min_max(block),
               spec=CHART_SPEC.get(cls.session_type, {}))

    if cls.session_type in ("easy", "trail_easy"):
        start, stop = find_work_block(block, ath)
        fit = fit_easy(t_min, block, slice(start, stop+1))
        out["easy_fit"] = fit
        out["work_stop_min"] = t_min[stop]
    elif cls.session_type == "tempo":
        sm = moving_average(block, SMOOTH_STRUCT_S)
        plateau = float(np.median(sm[sm >= np.percentile(sm,75)]))
        out["plateau"] = plateau
    elif cls.session_type in ("speed", "vo2max"):
        pk, tr = detect_peaks_troughs(block, ath)
        out["peaks_min"] = t_min[pk]; out["peaks_hr"] = block[pk]
        out["troughs_min"] = t_min[tr]; out["troughs_hr"] = block[tr]
        out["peak_env"] = fit_envelope(t_min[pk], block[pk]) if len(pk)>=3 else None
        out["trough_env"] = fit_envelope(t_min[tr], block[tr]) if len(tr)>=3 else None
    return out


if __name__ == "__main__":
    # smoke test with synthetic easy-like data
    rng = np.random.default_rng(0)
    base = 140 - 60*np.exp(-np.arange(2700)/180.0)
    hr = base + rng.normal(0, 2, size=base.shape)
    ath = Athlete()
    res = analyse(hr, 45, ath, "Treadmill running")
    print("type:", res["classification"].session_type)
    print("5min-max:", round(res["five_min_max"],1))
    if res.get("easy_fit"):
        print("asymptote:", round(res["easy_fit"]["asymptote"],1))
