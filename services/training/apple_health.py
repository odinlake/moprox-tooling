#!/usr/bin/env python3
"""Infer training sessions from an Apple Health 'Heart Rate' CSV (inferior devices, no start/stop).

Operator's heuristic: a session is a stretch where HR stays > HOT bpm, with no data gap > GAP_S and
brief recovery dips (<= LOWTOL_S at/below HOT tolerated), lasting >= MIN_DUR_S. HR is resampled to
1 Hz (linear interp over the irregular samples). These come from optical wrist devices we don't
trust to *type* a run, so they're kept as 'unknown' (shown as "other") with real stats — honest
about the source rather than guessing easy/tempo/etc.

CSV columns: Date/Time, Min, Max, Avg(=bpm), Context, Sources.
"""
import csv, datetime, os, sys
import numpy as np

HOT = 110          # "active" HR threshold (operator's heuristic)
GAP_S = 120        # data gap that ends a session
LOWTOL_S = 120     # tolerated time at/below HOT before the session ends (recovery dips)
MIN_DUR_S = 900    # 15 min minimum
TRACE_POINTS = 120

def parse(path):
    out = []
    with open(path, newline="") as f:
        r = csv.reader(f); next(r, None)
        for row in r:
            if len(row) < 4: continue
            try:
                ts = datetime.datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                bpm = float(row[3])
            except Exception:
                continue
            if 30 < bpm < 220:
                out.append((ts, bpm))
    out.sort(key=lambda x: x[0])
    return out

def _five_min_max(a):
    a = np.asarray(a, float)
    if len(a) < 300: return float(np.max(a)) if len(a) else 0.0
    c = np.cumsum(np.insert(a, 0, 0))
    return float(np.max((c[300:] - c[:-300]) / 300.0))

def _downsample(x, npts):
    x = np.asarray(x, float)
    if len(x) <= npts: return [round(float(v)) for v in x]
    step = len(x) / npts
    return [round(float(np.mean(x[int(j*step):max(int(j*step)+1, int((j+1)*step))]))) for j in range(npts)]

def detect(samples):
    """Yield (start_dt, secs, hr_1hz) for each inferred session."""
    ep = lambda dt: dt.timestamp()
    n = len(samples); i = 0
    while i < n:
        if samples[i][1] <= HOT: i += 1; continue
        last_hot = i; k = i + 1
        while k < n:
            if ep(samples[k][0]) - ep(samples[k-1][0]) > GAP_S: break          # data gap
            if samples[k][1] > HOT: last_hot = k
            elif ep(samples[k][0]) - ep(samples[last_hot][0]) > LOWTOL_S: break  # sustained recovery -> end
            k += 1
        t0, dur = samples[i][0], ep(samples[last_hot][0]) - ep(samples[i][0])
        if dur >= MIN_DUR_S:
            seg = samples[i:last_hot+1]
            xs = np.array([ep(s[0]) - ep(t0) for s in seg]); ys = np.array([s[1] for s in seg])
            yield t0, int(dur), np.interp(np.arange(0, int(dur)+1), xs, ys)
        i = max(k, last_hot + 1)

def sessions(path):
    out = []
    for t0, dur, hr in detect(parse(path)):
        block = np.clip(np.asarray(hr, float), 40, 210)
        out.append(dict(
            id="ah" + t0.strftime("%y%m%d%H%M"), date=t0.strftime("%Y-%m-%dT%H:%M:%S"), sport=1,
            cat="unknown", src="apple", dur_min=round(dur/60.0, 1),
            hr_avg=round(float(np.mean(block))), hr_max=round(float(np.max(block))),
            max5=round(_five_min_max(block)), nint=0, above_lt2=False, clamp=False, reps=[],
            trace=_downsample(block, TRACE_POINTS), trace_step_s=round(len(block)/min(len(block), TRACE_POINTS))))
    return out

if __name__ == "__main__":
    ss = sessions(sys.argv[1])
    print("inferred %d sessions" % len(ss))
    from collections import Counter
    print("per month:", dict(sorted(Counter(s["date"][:7] for s in ss).items())))
    durs = sorted(s["dur_min"] for s in ss)
    print("dur min/med/max: %.0f / %.0f / %.0f min" % (durs[0], durs[len(durs)//2], durs[-1]))
    for s in ss[:4]:
        print("  %s  %4.1f min  avg %d  max %d" % (s["date"][:16], s["dur_min"], s["hr_avg"], s["hr_max"]))
