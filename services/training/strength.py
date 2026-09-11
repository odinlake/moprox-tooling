#!/usr/bin/env python3
"""strength.py — turn the strength log into the dashboard's strength.json feed.

Deliberately a SIBLING of sessions.json, not a member of it. That array is built entirely around a
per-second HR trace (hr_avg, max5, floor, settled, climb, trace); a resistance session has none of
those, so it would be ~90% nulls with nothing to draw, and it would land in the per-category counts
and skew every aggregate computed over runs and rides.

What is published instead is what resistance work actually has: load, reps, sets, and time.

  strength.py [out_path]      default: ~/.cache/moprox-dashboard-data/training/strength.json
"""
import json, os, re, sys, time
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


# Every key in which this log says something about a set of resistance work. A row that carries NONE
# of them was never a strength entry, so "it is missing `sets`" is not a defect in it.
#
# This log holds two other things, both written deliberately and both growing. Cardio, as the
# warm-up that opens a resistance session: `treadmill-walk`, mins/kph/grade_pct. And free-standing
# session prose, attached to a movement name or to a marker of its own: an AMENDMENT to a rep count,
# an ORDER CORRECTION, a `jog-to-gym` disclosure, an `HR-TRACE-CAVEAT` addressed to whoever reads the
# Polar recording. Measured on the live log 2026-09-11: 7 rows of 40, on 3 separate dates, 3 of them
# written in the preceding 24 h. All 7 carry none of MEASURES; all 33 real entries carry `sets`. The
# split is clean on the whole corpus, which is why it is drawn on vocabulary and not on `ex`.
#
# Counting them as malformed put `row missing sets` at priority 4 on EVERY dashboard-update run —
# every 2 minutes, for ever, with a count that only goes up (3 -> 5 -> 6 -> 7 over 2026-09-10..11) —
# and it is one of the two lines holding the log-errors incident on that unit open. It is the shape
# lanes.json retired the amex-alert-detail lane for: a check asserting something nobody expects to be
# true again. Worse, it buries the case the warn exists for, because a genuinely malformed strength
# row now arrives as n+1 in a line that was already there.
#
# So they are separated, not silenced: counted as `notes`, published in the feed beside `skipped`,
# and stated on stdout on every run. A row that DOES speak strength and still misses `sets` is
# malformed, and warns exactly as before.
MEASURES = ("sets", "reps", "kg", "secs", "rir")


def entries():
    """(rows, skipped, notes). Rows that do not satisfy REQUIRED are dropped, not fatal."""
    if not LOG.exists():
        return [], 0, 0
    out, skipped, notes = [], 0, 0
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
            if all(r.get(k) is None for k in MEASURES):
                notes += 1          # cardio or prose: not an entry, and not an error either
                continue
            # Named, not counted-in-silence: this is how a writer using the wrong vocabulary gets
            # noticed instead of being rounded off.
            errlog.skip("strength.py: row missing %s" % ",".join(missing),
                        ValueError(json.dumps(r, ensure_ascii=False)[:200]))
            skipped += 1
            continue
        out.append(r)
    return sorted(out, key=lambda r: (r["date"], r.get("ts", ""))), skipped, notes


# Where a load CAME FROM, alongside the load itself. The log is a number plus prose, and the prose
# is where "I lifted this" and "the plan said this and nobody wrote down what was actually on the
# bar" have been living. On the only session the lane holds, three of seven rows say the latter —
# 72.5% of the session's volume and half its movement bests — and the panel printed them in the same
# column, in the same face, as the rows that were observed. `note` is not a field a renderer can act
# on; this is. Same shape and same job as `spd_src` on the runs feed.
# The log does not write that phrase the same way twice. 28 Aug says "load ASSUMED from
# prescription, not stated"; 8 Sep says "LOAD STILL ASSUMED from prescription: operator did not
# state the pin position" and "LOAD STILL ASSUMED: pin position not stated". A fixed two-word
# substring caught the first vocabulary and missed the second, and the miss does not fall back to
# "unknown" — it falls back to "stated", so an unparsed note ASSERTS the load was observed. That is
# the wrong direction to fail in, and it is what shipped: on the 8 Sep session the panel published
# seated-row 35 kg as the athlete's best with no mark on it while the row itself said the pin
# position was never stated.
#
# The invariant across every assumed row is "load ... assumed" inside ONE clause. Matching the bare
# word `assumed` would be wider and wrong in the other direction: the same session's db-chest-press
# row reads "CONFIRMED 16 kg on dumbbells (supersedes the 28 Aug ASSUMED figure)" — the one load the
# athlete did confirm — and would be relabelled a guess. `[\w\s]*` spans words but not punctuation,
# which is what keeps those apart.
#
# This is still a parser over prose, and the durable fix is upstream: a writer that sets `kg_src`
# outright, which kg_src() already prefers. It is the fallback that had to stop lying.
ASSUMED_NOTE = re.compile(r"\bload\b[\w\s]*\bassumed\b")


def kg_src(r):
    """"stated" | "assumed", or None when the row carries no load to attribute.

    An explicit `kg_src` on the row wins, so a writer that learns to say it outright never has to be
    parsed for; the note is the fallback for the rows already logged.
    """
    if load(r) is None:
        return None
    return r.get("kg_src") or ("assumed" if ASSUMED_NOTE.search((r.get("note") or "").lower())
                               else "stated")


# An explicit `kg: 0` is how this log spells "no external load", and it is not a load of zero.
# volume() below already commits to that reading in prose — bodyweight returns None rather than a
# zero — but every "is there a load" question was asked as `kg is None`, which is a different
# question, and the difference broke exactly the rows the contract was written for. The log spells
# one movement class both ways and the two spellings came out as two kinds: heel-raise-unloaded,
# logged with no `kg` at all, published kind "bodyweight" with a best of "10 reps", while
# heel-raise-bent-knee-DL — same calf-rehab block, note says "bodyweight" — carried `kg: 0` and
# published kind "weighted", kg_src "stated", volume 0.0, and a best the panel renders as "0 kg × 15".
# Measured on the live log 2026-09-11: four of the four rows logged that way, the whole of the
# current calf-rehab progression, and the coach's own note says the next rung is external load — so
# the first real dumbbell would extend a progression line from a zero nobody ever lifted.
# The writer is an agent composing free-form JSON and both spellings will keep arriving. The reader
# is where they have to mean the same thing.
def load(r):
    """The external load in kg, or None when the row carries none — absent or an explicit zero."""
    kg = r.get("kg")
    return None if kg is None or kg == 0 else kg


def volume(r):
    """sets x reps x kg, or None. Weighted movements only — an index, not kilos moved: 30 kg of
    pulldown is not 30 kg of squat, so it is comparable against itself over time and nothing else.
    Bodyweight and timed movements deliberately return None rather than a zero that would drag a
    session total down and look like a bad week."""
    kg = load(r)
    if kg is None or r.get("reps") is None:
        return None
    return round(float(r["sets"]) * float(r["reps"]) * float(kg), 1)


def build():
    rows, skipped, notes = entries()
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
            val = load(r) if k == "kg" else r.get(k)
            if val is not None:
                e[k] = val
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
            val = load(r) if k == "kg" else r.get(k)
            if val is not None:
                m[k] = val
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
            # Rows the log holds that this feed is not the reader for — cardio warm-ups and session
            # prose. Published for the same reason `skipped` is: the count is the only thing that
            # says the panel is showing fewer rows than the log. Unlike `skipped`, it is expected
            # to be non-zero, which is exactly why it must not be reported as a fault.
            "notes": notes,
            # Stated in the feed so the UI can label it honestly rather than implying kilograms.
            "volume_note": "volume load = sets x reps x kg; an index comparable only against itself",
            "sessions": sessions,
            "movements": mv}


def main(argv):
    out = Path(argv[0]) if argv else OUT
    data = build()
    out.parent.mkdir(parents=True, exist_ok=True)
    json.dump(data, open(out, "w"), separators=(",", ":"))
    print("strength: %d session(s), %d entries, %d movement(s), %d skipped, %d note/cardio row(s)"
          " -> %s" % (data["count"], data["entries"], len(data["movements"]), data["skipped"],
                      data["notes"], out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
