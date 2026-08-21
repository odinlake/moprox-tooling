#!/usr/bin/env python3
"""Append one health observation to the operator's health log.

WHY THIS EXISTS
  The operator mentions health in passing — "I've had a stubborn cold", "on antibiotics since
  Thursday", "tweaked my knee" — usually while talking about something else. Those remarks are the
  only context that explains what the ring and training data are doing (see the 2026-07 RHR step
  that got mistaken for hardware failure before the operator mentioned a cold). They were being
  lost the moment a session ended. This log is where any agent, ESPECIALLY coach, drops them.

RULES OF USE (see also private-data/health/README.md)
  - Record what the operator VOLUNTEERS. Never interrogate, never prompt for medical detail.
  - Record the observation, not a diagnosis. "sore throat, ongoing" — not "likely strep".
  - Absolute dates only. "since Thursday" said on a Sunday is `--date` two days back, not "Thursday".
    Enforced: a non-ISO --date exits 2 and writes nothing.
  - If the DATE came from the estate's own sensors rather than the operator, pass
    --date-source inferred. An inferred date that reads as reported is how a measurement
    becomes a circular inference; it has happened once already.
  - One line per distinct fact. A cold, a sore throat and a prescription are three lines.
  - Append-only. To correct a line, add a new one with --status corrected and say what changed.

  health_note.py --kind medication --date 2026-07-30 --status ongoing "amoxicillin"
  health_note.py --kind symptom "sore throat" --verbatim "A sore throat is with me"
"""
import argparse, json, os
from datetime import date, datetime
from pathlib import Path

LOG = Path.home() / "projects/private-data/health/hints.jsonl"

KINDS = ["symptom", "condition", "illness", "medication", "treatment", "injury",
         "sleep", "lifestyle", "appointment", "note"]

# Where `date` came from. "operator" = they said it, or said something ("end of June") that
# resolves to it. "inferred" = the estate derived it from its own sensors. The distinction is
# not cosmetic: the 2026-07-19 cold row was dated from the Ultrahuman RHR step, and that date
# was then used to rule illness out as the cause of the step. Unmarked, that is invisible.
DATE_SOURCES = ["operator", "inferred"]


def iso_date(s):
    """Absolute dates only — the one rule the log cannot recover from breaking."""
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{s!r} is not a YYYY-MM-DD date. Resolve 'Thursday'/'end of June' yourself "
            f"before writing; a relative date is worthless six months on.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", help="the observation, in plain words")
    ap.add_argument("--kind", choices=KINDS, default="note")
    ap.add_argument("--date", type=iso_date,
                    help="date the thing STARTED or applies to (YYYY-MM-DD); "
                         "default today. Resolve 'Thursday' yourself.")
    ap.add_argument("--date-source", choices=DATE_SOURCES, default="operator",
                    help="'inferred' if the estate derived the date from its own data rather "
                         "than the operator stating it. Say so; do not leave it looking reported.")
    ap.add_argument("--status", default="ongoing",
                    choices=["onset", "ongoing", "resolved", "corrected", "unknown"])
    ap.add_argument("--verbatim", help="operator's own words, when the phrasing carries nuance")
    ap.add_argument("--agent", default=os.environ.get("AGENT_ID", "unknown"))
    a = ap.parse_args()

    rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
           "date": a.date or date.today().isoformat(), "date_source": a.date_source,
           "agent": a.agent, "kind": a.kind, "status": a.status, "text": a.text}
    if a.verbatim:
        rec["verbatim"] = a.verbatim

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False))


if __name__ == "__main__":
    main()
