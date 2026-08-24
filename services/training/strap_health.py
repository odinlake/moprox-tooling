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
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog  # noqa: E402  — no silent swallows; see services/lib/errlog.py

DATA    = Path.home() / "projects/private-data/polar"
LOG     = DATA / "strap-health.jsonl"          # one record per session, append-only
BATTERY = DATA / "battery-changes.json"        # [{"date": "YYYY-MM-DD", "note": "..."}]
SPEC_HOURS = 400                               # H10 rated battery life
SETTLE_S   = 300                               # first 5 min: strap wetting/settling, not failure
STEADY     = (600, 2400)                       # 10:00-40:00, the window that is genuinely steady


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
    mid = [e for e in events if e[0] >= SETTLE_S]          # settling artifacts are not strap failure
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
    recs = sorted(records, key=lambda r: r.get("date", ""))
    if not recs: return "ok", "no sessions recorded"
    cur = recs[-1]
    hits = [r for r in recs if flagged(r)]
    last_change, hours = hours_since_change(recs, changes or [])
    age = ""
    if hours is not None:
        age = " Battery changed %s: %.0f h of sessions since (spec ~%d h)." % (last_change, hours, SPEC_HOURS)
        if hours < 0.25 * SPEC_HOURS:
            age += (" That is well inside spec, so a failing battery would point at the sensor being"
                    " left clipped to a damp strap between runs — detach it and it should last.")
    if not flagged(cur):
        return "ok", "strap clean (no mid-session dropouts, no 1 s jumps >=5 bpm)." + age
    recent = [r for r in hits if r["date"] < cur["date"]][-1:]
    signals = sum(1 for k in ("dropouts", "jumps5", "jumps8") if cur.get(k))
    near = recent and (_days(recent[0]["date"], cur["date"]) <= 14)
    what = ("%d mid-session dropout(s) totalling %d s (longest %d s)"
            % (cur["dropouts"], cur["dropout_s"], cur["longest_s"])) if cur["dropouts"] else ""
    if cur.get("jumps5"):
        what += (" and " if what else "") + "%d impossible 1 s jump(s) >=5 bpm in steady state" % cur["jumps5"]
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


def load_log():
    if not LOG.exists(): return []
    out = []
    for ln in LOG.read_text().splitlines():
        try: out.append(json.loads(ln))
        except Exception as e: errlog.skip("strap_health.py: log line", e)
    return out


def record(date, values):
    """Analyse a session, append it to the log (idempotent per date), and return (rec, level, msg)."""
    recs = load_log()
    rec = analyse(values); rec["date"] = date[:10]
    if not any(r.get("date") == rec["date"] for r in recs):
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f: f.write(json.dumps(rec) + "\n")
        recs.append(rec)
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
    else:
        recs = load_log()
        changes = json.loads(BATTERY.read_text()) if BATTERY.exists() else []
        for r in recs:
            if flagged(r):
                print("%s  dropouts=%d (%ds, longest %ds)  jumps>=5=%d"
                      % (r["date"], r["dropouts"], r["dropout_s"], r["longest_s"], r["jumps5"]))
        print("\n%d flagged of %d sessions" % (sum(1 for r in recs if flagged(r)), len(recs)))
        print("verdict: %s — %s" % verdict(recs, changes))
