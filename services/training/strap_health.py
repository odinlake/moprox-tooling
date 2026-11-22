#!/usr/bin/env python3
"""Chest-strap (Polar H10) health from the HR trace — is the battery going?

WHAT THE ONLINE ANSWER IS, AND WHY IT DOES NOT REACH US
  Polar's own account: a low battery shortens transmission range, which shows up as dropouts and
  erroneous readings. The battery LEVEL is readable — BLE Battery Service 0x180F / characteristic
  0x2A19, or the Polar BLE SDK's FEATURE_BATTERY_INFO — but only over Bluetooth, from a device in
  the room with the strap. The AccessLink API this estate pulls from serves finished training
  sessions and carries no battery field at all. Polar support also tells people to change the
  battery even when the app reports "Full", so the level is not trustworthy anyway.
  So: infer it from the trace, and treat that inference as weak evidence — see below.

WHAT THE TRACE CAN AND CANNOT SHOW (measured over 115 sessions, 2026-02-26..2026-08-24)
  Polar Beat's export is a smoothed 1 Hz product: 82% of consecutive samples are IDENTICAL and a
  1-second change of >=5 bpm occurs in 1 session out of 115. Short RF gaps are interpolated away
  before they reach the file. What survives is sparse — four genuine mid-session dropouts in six
  months (2026-04-16, 05-14, 05-28, 08-24), each 1-4 s, always straight to zero.
  THEREFORE: this module detects the failure, and does NOT promise to predict it. An honest search
  of the six months before the 2026-08-24 failure found no precursor that a rule could fire on.
  It is an instrument for the next cycle, not a forecaster of this one.

  Two traps this code exists to avoid, both of which produced a confident wrong answer first time:
  - "Frozen runs are dropouts": 60-98 plateaus of >=10 s occur in EVERY session, including the
    cleanest. Steady treadmill HR at 1 Hz integer resolution simply repeats. Not a signal.
  - "Below 100 bpm during the session is a dropout": counts the standing warm-up. The 2026-08-24
    session opens with a spurious 101 in the first two seconds, so an absolute rule scored 136 s of
    genuine 78-80 bpm warm-up as a dropout. Everything here is judged against a LOCAL baseline.

THE PREDICTOR THAT ACTUALLY WORKS IS USE, NOT SIGNAL
  H10 battery life is ~400 h. This estate knows every session's duration, so hours-since-change is
  exact and cheap. The catch is that the sensor keeps draining while clipped to a damp strap, so
  drain hours >> session hours: Polar's own advice is to detach it after every session. When hours
  since the last change are far below 400 and the battery is dying anyway, that IS the finding —
  it says the sensor is being left attached, which is fixable.
"""
import json, statistics as st, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))           # sport.py sits alongside this file
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog  # noqa: E402  — no silent swallows; see services/lib/errlog.py
import sport   # noqa: E402  — run | ride | strength | other: the estate's one sport authority

DATA    = Path.home() / "projects/private-data/polar"
LOG     = DATA / "strap-health.jsonl"          # one record per session, append-only
INCOMING = DATA / "incoming"                   # polar_fetch's raw store: {summary, hr} per exercise
BATTERY = DATA / "battery-changes.json"        # [{"date": "YYYY-MM-DD", "note": "..."}]
SPEC_HOURS = 400                               # H10 rated battery life
SETTLE_S   = 300                               # first 5 min: strap wetting/settling, not failure
STEADY     = (600, 2400)                       # 10:00-40:00, the window that is genuinely steady
CALIBRATED = "run"                             # the ONLY regime the thresholds above were measured in


def _num(x):
    try: return float(x)
    except (TypeError, ValueError): return 0.0


def analyse(values, half=45, drop=25, floor=110):
    """Per-session strap evidence. Every judgement is relative to the local median, never absolute."""
    hr = [_num(x) for x in values]
    n = len(hr)
    events, cur = [], None
    for i in range(n):
        w = [x for x in hr[max(0, i - half):i + half + 1] if x > 0]
        b = st.median(w) if w else 0
        bad = hr[i] == 0 or (b >= floor and hr[i] > 0 and hr[i] < b - drop)
        if bad:
            cur = [i, i, b, hr[i]] if cur is None else [cur[0], i, cur[2], min(cur[3], hr[i])]
        elif cur is not None:
            events.append(cur); cur = None
    if cur is not None: events.append(cur)
    # The settling guard is about WHICH SECONDS are artifacts, not which events, so it CLIPS an
    # event to the post-settling part instead of discarding it whole. Keyed on the start index it
    # scored the worst session in the record as clean: 2026-11-09 was 2619 of 2913 samples zero in
    # one run [0, 2618], and because that run was already in progress at t=300 it was worth zero
    # dropout seconds, `flagged()` was False and no STRAP HEALTH line was built — while the
    # operator's own battery-changes.json said "FAILED AGAIN" on the same date. Clipping agrees
    # with `zeros_mid`, which has always counted seconds (i >= SETTLE_S) rather than events: both
    # now read 2319 s on that session. An event ending before SETTLE_S lies wholly inside the
    # settling window and is still dropped, so the two 1 s pre-settling blips in the record
    # (2026-08-04 at 114 s, 2026-09-21 at 79 s) stay unflagged and no session gains a flag.
    # See moprox-memory/strap-onset-failure-discarded-whole.md.
    mid = [[max(e[0], SETTLE_S)] + e[1:] for e in events if e[1] >= SETTLE_S]
    steady = hr[STEADY[0]:STEADY[1]]
    dz = [abs(steady[i] - steady[i - 1]) for i in range(1, len(steady)) if steady[i] and steady[i - 1]]
    return {"minutes": round(n / 60.0, 1),
            "dropouts": len(mid),
            "dropout_s": sum(e[1] - e[0] + 1 for e in mid),
            "longest_s": max((e[1] - e[0] + 1 for e in mid), default=0),
            "first_at_s": mid[0][0] if mid else None,
            "zeros_mid": sum(1 for i, x in enumerate(hr) if x == 0 and i >= SETTLE_S),
            "jumps5": sum(1 for x in dz if x >= 5),         # 1 session in 115 has any
            "jumps8": sum(1 for x in dz if x >= 8),
            "repeat_pct": round(100 * sum(1 for x in dz if x == 0) / len(dz), 1) if dz else None}


def flagged(rec):
    """Did this session show anything the six-month record says is rare? (4 sessions in 115.)"""
    return bool(rec.get("dropouts") or rec.get("jumps5"))


def analysed(rec):
    """Was this row scored, or is it only on the drain clock? A usage row carries no analysis."""
    return "dropouts" in rec


def hours_since_change(records, changes):
    """Session hours since the last logged battery change — the real predictor, when it is logged."""
    if not changes: return None, None
    last = max(c["date"] for c in changes)
    h = sum(r["minutes"] for r in records if r.get("date", "") >= last) / 60.0
    return last, round(h, 1)


def verdict(records, changes=None):
    """(level, one-line message). Levels: ok | watch | replace.

    `replace` needs corroboration — two flagged sessions inside 14 days, or one session carrying
    several independent signals at once — because a single 1-second zero happens roughly every six
    weeks in a perfectly healthy strap and crying wolf on it would make this worthless.
    """
    # Two populations on purpose: the cell drains on every session the strap transmitted, but
    # every threshold here is run-calibrated, so only an analysed row may reach a judgement.
    everything = sorted(records, key=lambda r: r.get("date", ""))
    recs = [r for r in everything if analysed(r)]
    if not recs: return "ok", "no sessions analysed"
    cur = recs[-1]
    hits = [r for r in recs if flagged(r)]
    last_change, hours = hours_since_change(everything, changes or [])
    age = ""
    if hours is not None:
        age = " Battery changed %s: %.0f h of sessions since (spec ~%d h)." % (last_change, hours, SPEC_HOURS)
        if hours < 0.25 * SPEC_HOURS:
            age += (" That is well inside spec, so a failing battery would point at the sensor being"
                    " left clipped to a damp strap between runs — detach it and it should last.")
    if not flagged(cur):
        return "ok", "strap clean (no mid-session dropouts, no 1 s jumps >=5 bpm)." + age
    recent = [r for r in hits if r["date"] < cur["date"]][-1:]
    # ONE vote per independent family. analyse() counts jumps5 as #(dz>=5) and jumps8 as #(dz>=8)
    # over the same dz, so jumps8 is NESTED inside jumps5 — 0 of the log's 139 rows violate it, and
    # no trace can. Counting them as two made any jumps8>0 reach `replace` on a single phenomenon
    # counted twice: 2026-10-14 (dropouts=0, jumps5=6, jumps8=2) took this branch and told the
    # operator to replace a battery that had been changed that same day. jumps8 is a severity grade,
    # not corroboration, so it is REPORTED below instead of voting.
    signals = sum(1 for k in ("dropouts", "jumps5") if cur.get(k))
    near = recent and (_days(recent[0]["date"], cur["date"]) <= 14)
    what = ("%d mid-session dropout(s) totalling %d s (longest %d s)"
            % (cur["dropouts"], cur["dropout_s"], cur["longest_s"])) if cur["dropouts"] else ""
    if cur.get("jumps5"):
        what += (" and " if what else "") + "%d impossible 1 s jump(s) >=5 bpm in steady state" % cur["jumps5"]
        if cur.get("jumps8"):
            what += " (%d of them >=8 bpm)" % cur["jumps8"]
    if near:
        return "replace", ("STRAP BATTERY — %s, and the previous session with any was %s, %d days ago."
                           " Two inside a fortnight is the replace threshold.%s"
                           % (what, recent[0]["date"], _days(recent[0]["date"], cur["date"]), age))
    if signals >= 2:
        return "replace", ("STRAP BATTERY — %s in ONE session. Independent signals together, which is"
                           " what the 2026-08-24 failure looked like.%s" % (what, age))
    return "watch", ("strap: %s. Isolated — a healthy strap does this roughly every six weeks, so it is"
                     " noted, not acted on.%s" % (what, age))


def _days(a, b):
    f = "%Y-%m-%d"
    return abs(int((time.mktime(time.strptime(b[:10], f)) - time.mktime(time.strptime(a[:10], f))) // 86400))


def resolve(records):
    """Collapse a rescored date to its newest analysed row.

    The log is append-only and `record()` writes at most one analysed row per date, so a change to
    `analyse()` is FORWARD-ONLY: every stored row keeps whatever the code of its day computed, and
    the row that motivated the change is the one row it cannot reach. A correction is therefore a
    LATER row carrying `rescored_at`, never an edit, and this is where the later row wins.

    Only dates that carry such a row are collapsed, and only their ANALYSED rows: a usage row on
    the same date is a second session on the battery clock and passes through untouched (2026-11-09
    has one of each). A log with no `rescored_at` row is returned unchanged, so two analysed rows
    that arrived without one are left both standing rather than silently halved.
    See moprox-memory/append-only-log-fix-misses-its-own-row.md.
    """
    fixed = {r.get("date") for r in records if analysed(r) and r.get("rescored_at")}
    if not fixed: return list(records)
    out, seen = [], set()
    for r in reversed(records):
        if analysed(r) and r.get("date") in fixed:
            if r.get("date") in seen: continue
            seen.add(r.get("date"))
        out.append(r)
    out.reverse()
    return out


def load_log():
    if not LOG.exists(): return []
    out = []
    for ln in LOG.read_text().splitlines():
        try: out.append(json.loads(ln))
        except Exception as e: errlog.skip("strap_health.py: log line", e)
    return resolve(out)


def run_traces():
    """date -> (trace path, hr) for every date the raw store holds exactly ONE run trace for.

    `record()` keeps the first analysed row it is given for a date, so a date with two run traces
    does not identify which session the stored row came from. Those are returned separately and
    skipped rather than guessed (0 of 67 run dates in the store on 2026-11-22).
    """
    seen = {}
    for p in sorted(INCOMING.glob("exercise_*.json")):
        try: d = json.loads(p.read_text())
        except Exception as e:
            errlog.skip("strap_health.py: raw trace %s" % p.name, e); continue
        s = d.get("summary") or {}
        if sport.kind(sport.ex_label(s)) != CALIBRATED: continue
        date = (s.get("start_time") or "")[:10]
        if date: seen.setdefault(date, []).append((p, d.get("hr") or []))
    ambiguous = {k: [p.name for p, _ in v] for k, v in seen.items() if len(v) > 1}
    return {k: v[0] for k, v in seen.items() if len(v) == 1}, ambiguous


def rescore(write=False):
    """Recompute every stored analysed row from its raw trace and report what its own code got wrong.

    Without this there is no path from a fixed `analyse()` to a stored row at all: 385953af
    (2026-11-11) made a dropout starting inside the settling window clip instead of vanish, and
    11 days later 2026-11-09 still read `dropouts=0, dropout_s=0` beside its own `zeros_mid=2319`,
    so the estate's worst recorded strap session was absent from `report`'s flagged census. A
    replay cannot repair it: the per-date guard in `record()` makes the replay a no-op, and
    removing the guard duplicates the row rather than correcting it.
    """
    recs = load_log()
    traces, ambiguous = run_traces()
    stale, missing = [], []
    for r in recs:
        if not analysed(r): continue
        t = traces.get(r.get("date"))
        if t is None: missing.append(r.get("date")); continue
        new = analyse(t[1]); new["date"] = r["date"]
        diff = sorted(k for k in new if k != "date" and r.get(k) != new[k])
        if diff: stale.append((r, new, diff, t[0].name))
    for r, new, diff, name in stale:
        print("%s STALE on %s: %s  [%s]"
              % (r["date"], name, ", ".join("%s %r->%r" % (k, r.get(k), new[k]) for k in diff),
                 "rescored" if write else "dry run, pass --write"))
        if write:
            new["rescored_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            new["rescored_from"] = name
            _append(new)
    for date, names in sorted(ambiguous.items()):
        print("%s NOT rescored: %d run traces in the raw store (%s) — the stored row names none of"
              " them, so correcting it would be a guess" % (date, len(names), ", ".join(names)))
    print("%d stale of %d analysed row(s) with a trace (%d row(s) have no trace in the raw store,"
          " which keeps ~90 days)" % (len(stale), sum(1 for r in recs if analysed(r)) - len(missing),
                                      len(missing)))
    return stale


def _append(row):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f: f.write(json.dumps(row) + "\n")


def record(date, values, sport_label):
    """Analyse a session, append it to the log (idempotent per date), and return (rec, level, msg).

    `rec` is None when the session was NOT analysed, and only a `run` is. Every threshold above was
    measured over 115 all-treadmill sessions, and `jumps5` turns out to be determined by the SPORT
    rather than by the strap: on strength work it fires in 12 of 14 sessions against the docstring's
    1-in-115, the split is total within the same day on the same strap at the same charge (11/11
    strength vs 0/11 cardio over the 11 paired dates), and `repeat_pct` separates disjointly. A
    failing battery is monotone in time and use and is shared by two sessions on one day; this is
    neither. The cost was paid: the only non-ok verdict the estate has raised since 2026-09-01 came
    off the 2026-09-10 STRENGTH_TRAINING trace and reached coach as a top-line "say this to Mikael"
    STRAP BATTERY instruction. See moprox-memory/strap-jumps5-is-a-sport-artefact.md.

    Scoring a non-run against a treadmill calibration is not evidence about the strap, so it is
    REPORTED and not analysed — never silently dropped — and the level returned is the strap's state
    as last MEASURED, over the run sessions in the log. Recalibrating per sport is the alternative
    and n=14 cannot support it. `sport_label` is required on purpose: there is no default that is
    safe, since guessing `run` resurrects this bug and guessing anything else disables the lane.
    """
    recs = load_log()
    kind = sport.kind(sport_label)
    rec = None
    if kind == CALIBRATED:
        rec = analyse(values); rec["date"] = date[:10]
        if not any(analysed(r) and r.get("date") == rec["date"] for r in recs):
            _append(rec); recs.append(rec)
    else:
        # NOT scored — the thresholds are run-calibrated — but it drained the same cell, and
        # hours_since_change is the one predictor this module says actually works. So the clock
        # gets it without the analysis. Deduped on the whole row: a replay is idempotent, and a
        # second session of the same kind on one day is still a second session on the clock.
        row = {"date": date[:10], "minutes": round(len(values) / 60.0, 1), "sport": kind}
        if row not in recs:
            _append(row); recs.append(row)
        print("strap: %s not analysed — %r is a %s session and every threshold here is %s-calibrated;"
              " its %.1f min go on the battery clock, verdict stands on the %d session(s) measured"
              % (date[:10], sport_label, kind, CALIBRATED, row["minutes"],
                 sum(1 for r in recs if analysed(r))))
    changes = json.loads(BATTERY.read_text()) if BATTERY.exists() else []
    level, msg = verdict(recs, changes)
    return rec, level, msg


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "battery-changed":
        d = sys.argv[2]
        cur = json.loads(BATTERY.read_text()) if BATTERY.exists() else []
        cur.append({"date": d, "note": " ".join(sys.argv[3:]) or "logged by strap_health"})
        BATTERY.write_text(json.dumps(cur, indent=1)); print("logged battery change", d)
    elif cmd == "rescore":
        rescore(write="--write" in sys.argv[2:])
    else:
        recs = load_log()
        changes = json.loads(BATTERY.read_text()) if BATTERY.exists() else []
        for r in recs:
            if flagged(r):
                print("%s  dropouts=%d (%ds, longest %ds)  jumps>=5=%d"
                      % (r["date"], r["dropouts"], r["dropout_s"], r["longest_s"], r["jumps5"]))
        print("\n%d flagged of %d analysed (%d session(s) on the battery clock)"
              % (sum(1 for r in recs if flagged(r)), sum(1 for r in recs if analysed(r)), len(recs)))
        print("verdict: %s — %s" % verdict(recs, changes))
