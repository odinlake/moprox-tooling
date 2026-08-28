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


def entries():
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError as e:
            errlog.skip("strength.py: log line", e)      # one bad line must not lose the rest
            continue
        if r.get("ex") and r.get("date"):
            out.append(r)
    return sorted(out, key=lambda r: (r["date"], r.get("ts", "")))


def volume(r):
    """sets x reps x kg, or None. Weighted movements only — an index, not kilos moved: 30 kg of
    pulldown is not 30 kg of squat, so it is comparable against itself over time and nothing else.
    Bodyweight and timed movements deliberately return None rather than a zero that would drag a
    session total down and look like a bad week."""
    if r.get("kg") is None or r.get("reps") is None:
        return None
    return round(float(r["sets"]) * float(r["reps"]) * float(r["kg"]), 1)


def build():
    rows = entries()
    by_date = OrderedDict()
    movements = defaultdict(list)

    for r in rows:
        d = r["date"]
        s = by_date.setdefault(d, {"date": d, "entries": [], "sets": 0,
                                   "volume_load": 0.0, "has_unweighted": False})
        v = volume(r)
        e = {"ex": r["ex"], "sets": r["sets"]}
        for k in ("reps", "kg", "secs", "rir", "note"):
            if r.get(k) is not None:
                e[k] = r[k]
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
    # actually looks for. "Best" is top load, tie-broken by reps — 30x10 beats 30x8.
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
            # Stated in the feed so the UI can label it honestly rather than implying kilograms.
            "volume_note": "volume load = sets x reps x kg; an index comparable only against itself",
            "sessions": sessions,
            "movements": mv}


def main(argv):
    out = Path(argv[0]) if argv else OUT
    data = build()
    out.parent.mkdir(parents=True, exist_ok=True)
    json.dump(data, open(out, "w"), separators=(",", ":"))
    print("strength: %d session(s), %d entries, %d movement(s) -> %s"
          % (data["count"], data["entries"], len(data["movements"]), out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
