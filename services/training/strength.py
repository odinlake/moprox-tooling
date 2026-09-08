#!/usr/bin/env python3
"""strength.py — turn the strength log into the dashboard's strength.json feed.

Deliberately a SIBLING of sessions.json, not a member of it. That array is built entirely around a
per-second HR trace (hr_avg, max5, floor, settled, climb, trace); a resistance session has none of
those, so it would be ~90% nulls with nothing to draw, and it would land in the per-category counts
and skew every aggregate computed over runs and rides.

What is published instead is what resistance work actually has: load, reps, sets, and time.

  strength.py [out_path]      default: ~/.cache/moprox-dashboard-data/training/strength.json
"""
import json, os, sys, time
from collections import OrderedDict, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog

LOG = Path(os.environ.get("STRENGTH_LOG",
                          Path.home() / "projects/private-data/training/strength.jsonl"))
OUT = Path(os.environ.get("STRENGTH_OUT",
                          Path.home() / ".cache/moprox-dashboard-data/training/strength.json"))


# Every field this module dereferences without a .get(). The gate below used to check `ex` and
# `date` only, while build() and volume() went on to read r["sets"] outright — so a line that was
# perfectly valid JSON but carried a different vocabulary did not lose ITSELF, it lost the whole
# feed. On 2026-09-08 one such line (a treadmill warm-up logged as mins/kph/grade_pct, no sets)
# killed every run of strength.py from 12:52 UTC on, and update.py's non-fatal wrapper meant the
# dashboard just went on serving the previous strength.json — 12 well-formed rows from that day's
# session, including the load confirmation that supersedes an assumed one, invisible with no
# outward sign. The gate now covers exactly what the code requires, and a row that misses it is
# skipped and counted, per this module's own "one bad line must not lose the rest".
REQUIRED = ("ex", "date", "sets")


def entries():
    """(rows, skipped). Rows that do not satisfy REQUIRED are dropped, not fatal."""
    if not LOG.exists():
        return [], 0
    out, skipped = [], 0
    for line in LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError as e:
            errlog.skip("strength.py: log line", e)      # one bad line must not lose the rest
            skipped += 1
            continue
        missing = [k for k in REQUIRED if r.get(k) is None]
        if missing:
            # Named, not counted-in-silence: this is how a writer using the wrong vocabulary gets
            # noticed instead of being rounded off.
            errlog.skip("strength.py: row missing %s" % ",".join(missing),
                        ValueError(json.dumps(r, ensure_ascii=False)[:200]))
            skipped += 1
            continue
        out.append(r)
    return sorted(out, key=lambda r: (r["date"], r.get("ts", ""))), skipped


# Where a load CAME FROM, alongside the load itself. The log is a number plus prose, and the prose
# is where "I lifted this" and "the plan said this and nobody wrote down what was actually on the
# bar" have been living. On the only session the lane holds, three of seven rows say the latter —
# 72.5% of the session's volume and half its movement bests — and the panel printed them in the same
# column, in the same face, as the rows that were observed. `note` is not a field a renderer can act
# on; this is. Same shape and same job as `spd_src` on the runs feed.
ASSUMED_NOTE = "load assumed"


def kg_src(r):
    """"stated" | "assumed", or None when the row carries no load to attribute.

    An explicit `kg_src` on the row wins, so a writer that learns to say it outright never has to be
    parsed for; the note is the fallback for the rows already logged.
    """
    if r.get("kg") is None:
        return None
    return r.get("kg_src") or ("assumed" if ASSUMED_NOTE in (r.get("note") or "").lower()
                               else "stated")


def volume(r):
    """sets x reps x kg, or None. Weighted movements only — an index, not kilos moved: 30 kg of
    pulldown is not 30 kg of squat, so it is comparable against itself over time and nothing else.
    Bodyweight and timed movements deliberately return None rather than a zero that would drag a
    session total down and look like a bad week."""
    if r.get("kg") is None or r.get("reps") is None:
        return None
    return round(float(r["sets"]) * float(r["reps"]) * float(r["kg"]), 1)


def build():
    rows, skipped = entries()
    by_date = OrderedDict()
    movements = defaultdict(list)

    for r in rows:
        d = r["date"]
        s = by_date.setdefault(d, {"date": d, "entries": [], "sets": 0,
                                   "volume_load": 0.0, "has_unweighted": False})
        v = volume(r)
        src = kg_src(r)
        e = {"ex": r["ex"], "sets": r["sets"]}
        for k in ("reps", "kg", "secs", "rir", "note"):
            if r.get(k) is not None:
                e[k] = r[k]
        if src:
            e["kg_src"] = src
        if v is not None:
            e["volume"] = v
            s["volume_load"] += v
        else:
            s["has_unweighted"] = True
        s["entries"].append(e)
        s["sets"] += int(r["sets"] or 0)

        m = {"date": d, "sets": r["sets"]}
        for k in ("reps", "kg", "secs", "rir"):
            if r.get(k) is not None:
                m[k] = r[k]
        if src:
            m["kg_src"] = src
        if v is not None:
            m["volume"] = v
        movements[r["ex"]].append(m)

    sessions = []
    for d, s in by_date.items():
        s["volume_load"] = round(s["volume_load"], 1) or None
        s["movements"] = len({e["ex"] for e in s["entries"]})
        sessions.append(s)
    sessions.sort(key=lambda s: s["date"], reverse=True)

    # Per movement: the progression line, plus the current best, which is the number the athlete
    # actually looks for. "Best" is top load, tie-broken by reps — 30x10 beats 30x8. An assumed load
    # can and does win that comparison against a stated one on the same day, so `best` carries its
    # own `kg_src` through from the row and the panel has to show it.
    mv = {}
    for ex, hist in movements.items():
        hist.sort(key=lambda h: h["date"])
        weighted = [h for h in hist if h.get("kg") is not None]
        timed = [h for h in hist if h.get("secs") is not None]
        best = None
        if weighted:
            best = max(weighted, key=lambda h: (h["kg"], h.get("reps") or 0))
        elif timed:
            best = max(timed, key=lambda h: h["secs"])
        elif hist:
            best = max(hist, key=lambda h: h.get("reps") or 0)
        mv[ex] = {"history": hist, "best": best, "n": len(hist),
                  "kind": "weighted" if weighted else ("timed" if timed else "bodyweight"),
                  "last": hist[-1] if hist else None}

    return {"generated": int(time.time()),
            "count": len(sessions),
            "entries": len(rows),
            # In the feed, not only in the journal: a panel that quietly shows fewer rows than the
            # log holds is the thing this module just failed at. 0 is the normal case.
            "skipped": skipped,
            # Stated in the feed so the UI can label it honestly rather than implying kilograms.
            "volume_note": "volume load = sets x reps x kg; an index comparable only against itself",
            "sessions": sessions,
            "movements": mv}


def main(argv):
    out = Path(argv[0]) if argv else OUT
    data = build()
    out.parent.mkdir(parents=True, exist_ok=True)
    json.dump(data, open(out, "w"), separators=(",", ":"))
    print("strength: %d session(s), %d entries, %d movement(s), %d skipped -> %s"
          % (data["count"], data["entries"], len(data["movements"]), data["skipped"], out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
