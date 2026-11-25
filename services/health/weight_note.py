#!/usr/bin/env python3
"""Append one body-weight reading to the operator's weight log (private-data/health/weight.jsonl).

WHY THIS EXISTS
  The log was hand-written by coach and had no sub-day field: `date` is a bare day, and the clock
  went into the note as prose ("logged 06:12") on 26 of its first 30 rows. Anything reading the
  file by field saw every row at midnight (i-20261125-201202). This writer stamps `ts` on every
  row, unconditionally, the way health_note.py does for hints.jsonl.

  `ts` is WHEN THE ROW WAS RECORDED (what "logged HH:MM" meant), full ISO with the zone offset.
  There is no flag to set it. Do not type the clock into --note.

RULES OF USE (see also private-data/health/README.md, "weight.jsonl")
  - --time is required and is never guessed from the clock: say morning or evening as the
    operator means it, or `unknown` (written as null).
  - Two scales. The Salter is the primary series and the default. A Tanita reading is
    `--scale tanita` and is written with "reference": true, which readers exclude from means.
  - --approx is for a figure the operator quoted from memory, not a scale reading.
  - Append-only. One call, one row.

  weight_note.py --kg 73.4 --time morning
  weight_note.py --kg 75.4 --time evening --scale tanita --note "same weigh-in as the Salter 75.0"
"""
import argparse, json, os, sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog

LOG = Path(os.environ.get("WEIGHT_LOG",
                          Path.home() / "projects/private-data/health/weight.jsonl"))

KG_MIN, KG_MAX = 40.0, 150.0


def iso_date(s):
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"{s!r} is not a YYYY-MM-DD date")


def kg(s):
    """A plausible adult body weight. A typo (734, 7.34) is refused, not logged."""
    try:
        v = float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{s!r} is not a number")
    if not KG_MIN <= v <= KG_MAX:      # also rejects nan
        raise argparse.ArgumentTypeError(f"{s} kg is outside {KG_MIN:g}-{KG_MAX:g}")
    return v


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--kg", type=kg, required=True)
    ap.add_argument("--time", required=True, choices=["morning", "evening", "unknown"],
                    help="which weigh-in this is; never inferred from the clock. "
                         "'unknown' writes null")
    ap.add_argument("--scale", choices=["salter", "tanita"], default="salter",
                    help="tanita rows are also written with reference: true")
    ap.add_argument("--date", type=iso_date,
                    help="day of the weigh-in (YYYY-MM-DD); default today, local time")
    ap.add_argument("--approx", action="store_true",
                    help="a remembered or rounded figure, not a scale reading")
    ap.add_argument("--note", default="", help="context only; the clock belongs in ts, not here")
    ap.add_argument("--agent", default=os.environ.get("AGENT_ID", "unknown"))
    a = ap.parse_args()

    rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
           "date": a.date or date.today().isoformat(),
           "time": None if a.time == "unknown" else a.time,
           "kg": a.kg, "approx": a.approx, "source": "operator", "agent": a.agent,
           "scale": a.scale}
    if a.scale == "tanita":
        rec["reference"] = True
    rec["note"] = a.note

    line = json.dumps(rec, ensure_ascii=False) + "\n"
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(line)
    except OSError as e:
        errlog.die(f"weight_note: could not append to {LOG}; the reading was NOT logged", e)
    print(line, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
