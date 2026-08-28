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
"""
import argparse, json, os, sys, time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog

LOG = Path(os.environ.get("STRENGTH_LOG",
                          Path.home() / "projects/private-data/training/strength.jsonl"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Append one movement to the strength log.")
    ap.add_argument("--ex", required=True, help="movement, lowercase-hyphenated (seated-row, goblet-squat)")
    ap.add_argument("--sets", type=int, required=True)
    ap.add_argument("--reps", type=int, help="reps per set (omit for a timed movement)")
    ap.add_argument("--kg", type=float, help="load per set; omit for bodyweight")
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

    rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
           "date": a.date or time.strftime("%Y-%m-%d"),
           "ex": a.ex.strip().lower().replace(" ", "-"), "sets": a.sets}
    for k, v in (("reps", a.reps), ("kg", a.kg), ("secs", a.secs), ("rir", a.rir),
                 ("note", a.note or None), ("agent", a.agent or None)):
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
