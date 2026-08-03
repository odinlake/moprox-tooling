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
  - One line per distinct fact. A cold, a sore throat and a prescription are three lines.
  - Append-only. To correct a line, add a new one with --status corrected and say what changed.

  health_note.py --kind medication --date 2026-07-30 --status ongoing "amoxicillin"
  health_note.py --kind symptom "sore throat" --verbatim "A sore throat is with me"
"""
import argparse, json, os
from datetime import date, datetime
from pathlib import Path

LOG = Path.home() / "projects/private-data/health/hints.jsonl"

KINDS = ["symptom", "condition", "medication", "treatment", "injury",
         "sleep", "lifestyle", "appointment", "note"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", help="the observation, in plain words")
    ap.add_argument("--kind", choices=KINDS, default="note")
    ap.add_argument("--date", help="date the thing STARTED or applies to (YYYY-MM-DD); "
                                   "default today. Resolve 'Thursday' yourself.")
    ap.add_argument("--status", default="ongoing",
                    choices=["onset", "ongoing", "resolved", "corrected", "unknown"])
    ap.add_argument("--verbatim", help="operator's own words, when the phrasing carries nuance")
    ap.add_argument("--agent", default=os.environ.get("AGENT_ID", "unknown"))
    a = ap.parse_args()

    rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
           "date": a.date or date.today().isoformat(),
           "agent": a.agent, "kind": a.kind, "status": a.status, "text": a.text}
    if a.verbatim:
        rec["verbatim"] = a.verbatim

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False))


if __name__ == "__main__":
    main()
