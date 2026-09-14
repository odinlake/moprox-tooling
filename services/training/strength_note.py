#!/usr/bin/env python3
"""strength_note.py — append one movement to the strength log.

Resistance work has no HR trace, so it cannot live in the dashboard's sessions[] (which is built
entirely around one). It gets its own append-only log here and its own feed via strength.py.

ONE LINE PER MOVEMENT, not per session. A session is whatever shares a date, assembled at build
time. That keeps this a pure append — no read-modify-write — so it is safe to call mid-workout,
from two places at once, and by whoever is asked (the operator, the dev session, coach).

Three movement shapes, because a real session has all three:
  weighted   --ex seated-row   --sets 2 --reps 10 --kg 35
  bodyweight --ex push-up      --sets 2 --reps 12
  timed      --ex plank        --sets 2 --secs 45

Volume load (sets x reps x kg) is computed downstream and ONLY for the weighted shape. It is an
index, not a physical quantity — 30 kg of pulldown is not 30 kg of squat — so it is comparable
against itself over time and nothing else. Per-movement load is the honest view.

  strength_note.py --ex lat-pulldown --sets 2 --reps 9 --kg 30 --rir 2
  strength_note.py --ex plank --sets 2 --secs 45 --note "at limit"

A load that was NOT read back off the machine is marked as such, with --kg-src assumed:

  strength_note.py --ex seated-row --sets 2 --reps 10 --kg 35 --kg-src assumed
"""
import argparse, json, os, sys, time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog

LOG = Path(os.environ.get("STRENGTH_LOG",
                          Path.home() / "projects/private-data/training/strength.jsonl"))


# Where the load CAME FROM, as a field rather than as a sentence. A pulldown or row is a pin the
# operator may never read back, so "35 kg" in this log is sometimes an observation and sometimes the
# coach's own prescription written forward. strength.py publishes that distinction as `kg_src` and
# prefers an explicit one on the row — but until now nothing could write one, so the only way to say
# "assumed" was to phrase a note matching its regex, and an unparsed note does not fall back to
# unknown: it falls back to "stated", asserting an observation nobody made.
#
# Measured on the live log 2026-09-14 (moprox-memory/strength-adherence-unobserved-load): 8 of the
# 20 load cells are assumed, all four seated-row dates and three of four lat-pulldown dates among
# them, and for those movements "the weight was held" and "the weight was never reported" are the
# same record — 5 of 13 next-session directives cannot score a miss. The prose parser agrees with an
# independent reading on all 28 rows today; it is one rephrasing away from not doing, and it fails
# toward the claim. This is the field that ends the parse.
KG_SRC = ("stated", "assumed")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Append one movement to the strength log.")
    ap.add_argument("--ex", required=True, help="movement, lowercase-hyphenated (seated-row, goblet-squat)")
    ap.add_argument("--sets", type=int, required=True)
    ap.add_argument("--reps", type=int, help="reps per set (omit for a timed movement)")
    ap.add_argument("--kg", type=float, help="load per set; omit for bodyweight")
    ap.add_argument("--kg-src", choices=KG_SRC,
                    help="where --kg came from: 'stated' (read off the machine) or 'assumed' "
                         "(the prescription, not read back). Omit if you do not know.")
    ap.add_argument("--secs", type=int, help="seconds per set, for timed movements (plank etc)")
    ap.add_argument("--rir", type=float, help="reps in reserve — how many were left. 0 = to failure")
    ap.add_argument("--date", help="YYYY-MM-DD; default today. Resolve 'Friday' yourself.")
    ap.add_argument("--note", default="")
    ap.add_argument("--agent", default=os.environ.get("AGENT_ID", ""))
    a = ap.parse_args(argv)

    if a.reps is None and a.secs is None:
        ap.error("give --reps (weighted or bodyweight) or --secs (timed)")
    if a.reps is not None and a.secs is not None:
        ap.error("--reps and --secs are different movement shapes; give one")
    # `kg: 0` is how this log spells "no external load", not a load of zero, and strength.py's
    # load() reads it that way — so it attributes no source for either spelling and would drop the
    # field. Rejecting here is the difference between the writer knowing that and believing it
    # recorded a provenance it did not.
    if a.kg_src and not a.kg:
        ap.error("--kg-src describes --kg; give a non-zero --kg or drop it")

    rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
           "date": a.date or time.strftime("%Y-%m-%d"),
           "ex": a.ex.strip().lower().replace(" ", "-"), "sets": a.sets}
    for k, v in (("reps", a.reps), ("kg", a.kg), ("kg_src", a.kg_src), ("secs", a.secs),
                 ("rir", a.rir), ("note", a.note or None), ("agent", a.agent or None)):
        if v is not None:
            rec[k] = v

    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        errlog.err(f"strength_note: could not append to {LOG}", e)
        raise

    print(json.dumps(rec, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
