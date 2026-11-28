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
  - Every Tanita row carries "model", written here without being asked: HD-395 (the current
    Tanita, TANITA_MODELS[0]) unless `--model BC-730` says the old one was used. Do not put the
    model in --note. The Salter's model is not on record: its rows have no "model", and --model
    with the Salter is refused (i-20261126-071240).
  - --approx is for a figure the operator quoted from memory, not a scale reading.
  - --exclude REASON marks a reading that is real but must be left out of means and trends,
    e.g. `--exclude illness` (fever, not eating or drinking normally). REASON is one short
    lowercase token, written as "exclude": "<reason>"; the detail goes in --note. Do not mark
    this in the note's prose ("ILL"): nothing reads that (i-20261125-210927).
  - Append-only. One call, one row.

  weight_note.py --kg 73.4 --time morning
  weight_note.py --kg 75.4 --time evening --scale tanita --note "same weigh-in as the Salter 75.0"
  weight_note.py --kg 72.4 --time morning --exclude illness --note "fever 38 C since yesterday"
"""
import argparse, json, os, re, sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog

LOG = Path(os.environ.get("WEIGHT_LOG",
                          Path.home() / "projects/private-data/health/weight.jsonl"))

KG_MIN, KG_MAX = 40.0, 150.0

# The same gate as health_note.py's, on the same field, for the log next to it. `agent` is this
# row's only provenance key and took whatever the caller typed; this log has not forked and the
# only reason is that its one documented invocation spells out `--agent coach`, while both logs
# are written by the same session. On 2026-11-25 the coach session started typing its PERSONA name --
# `--agent odinlake-ai-coach`, which is what dev-launch.sh's prompt and agents/coach/CLAUDE.md
# call it -- in place of its agent id, mid-episode and inside one session instance. The live
# illness runs to 14 rows: 3 under `coach` (reading as over on the morning of the 25th) and 11
# under the new spelling (missing the onset row and the 38 C reading), so NEITHER spelling
# returns the episode. moprox-memory/health-hints-writer-identity-forked.
#
# TWO registries, because neither half is this estate's vocabulary on its own and a gate on one
# half is a regression. `private-data/agents/<id>/` is where a NAMED agent has its directory --
# coach, analyst, burndown and nine others. The three shared-context dev sessions are not there:
# moprox-dev@.service exports AGENT_ID=%i, and one|two|three share one working directory and one
# memory dir, so they have no directory of their own (services/sessions/README.md). They are this
# log's 2nd and 3rd most prolific writers -- `one` 16 rows, `two` 8 of 132 -- so a gate reading
# only the directories would refuse the dev sessions their OWN exported id and drop the
# observation. The instances are declared by the unit files beside this script, which is where
# %i comes from.
#
# "unknown" stays legal. It is not a claim about a writer; it is the absence AGENT_ID leaves when
# nobody exported one, exactly as `date_source: unknown` is the absence of a date's provenance,
# and refusing it would cost a hand-run its observation to protect a field nothing reads yet.
# Read from Path.home() and NOT from LOG, which $WEIGHT_LOG moves for the tests: the registry
# is a property of the estate, not of wherever the log was pointed.
WHO = "weight_note"
AGENT_DIRS = Path.home() / "projects/private-data/agents"
SESSION_UNITS = Path(__file__).resolve().parents[1] / "sessions"
UNKNOWN_AGENT = "unknown"

# The first entry is the Tanita in use and the default. When the reference device changes
# again, add the new model at the front: nothing else needs to change.
TANITA_MODELS = ("HD-395", "BC-730")


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


def reason(s):
    """One short lowercase token (illness, travel, ...). Free text belongs in --note."""
    if not re.fullmatch(r"[a-z][a-z-]{1,19}", s):
        raise argparse.ArgumentTypeError(
            f"{s!r} is not a reason token (lowercase letters and '-', 2-20 chars, e.g. illness); "
            "put the detail in --note")
    return s


def known_agents():
    """This estate's agent ids: the agents/ directories, plus the moprox-dev@ instance ids.

    [] if EITHER registry comes up empty. A half-read vocabulary is not a vocabulary -- with the
    directories alone the dev sessions are strangers, with the units alone every named agent is --
    and refusing a legitimate id costs the observation. The caller reports that case at err.

    Deliberately duplicated in weight_note.py rather than shared, for the same reason rid() is
    duplicated from strength.py: it is two directory listings, and the alternative is a new module
    under lib/ that six long-lived daemons import at module scope. Extract at a third writer.
    """
    try:
        named = {p.name for p in AGENT_DIRS.iterdir() if p.is_dir()}
    except OSError:
        named = set()
    # moprox-dev@<i>.conf, moprox-dev-cycle@<i>.timer: the instance sits between the @ and the
    # suffix, and a template (`moprox-dev@.service`) leaves it empty.
    insts = {f.name.split("@", 1)[1].rsplit(".", 1)[0]
             for f in SESSION_UNITS.glob("moprox-dev*@*.*")}
    insts.discard("")
    if not named or not insts:
        return []
    return sorted(named | insts)


def check_agent(ap, agent):
    """Refuse an `agent` this estate has no id for. Nothing is appended on a refusal.

    Unvalidatable is not the same as valid. If the vocabulary cannot be read the observation is
    still worth more than its provenance, so the row IS written -- but that reaches the journal at
    err, because a provenance key that quietly stopped being checked is how this field forked in
    the first place.
    """
    if agent == UNKNOWN_AGENT:
        return
    known = known_agents()
    if not known:
        errlog.err("%s: cannot read this estate's agent ids (%s + %s), so --agent %r was written "
                   "UNCHECKED" % (WHO, AGENT_DIRS, SESSION_UNITS, agent))
        return
    if agent not in known:
        ap.error("--agent %s is not an agent id of this estate (%s).\nPass your AGENT_ID, not the "
                 "persona name a prompt gave you; omitting --agent uses AGENT_ID=%s."
                 % (agent, ", ".join(known), os.environ.get("AGENT_ID", UNKNOWN_AGENT)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--kg", type=kg, required=True)
    ap.add_argument("--time", required=True, choices=["morning", "evening", "unknown"],
                    help="which weigh-in this is; never inferred from the clock. "
                         "'unknown' writes null")
    ap.add_argument("--scale", choices=["salter", "tanita"], default="salter",
                    help="tanita rows are also written with reference: true")
    ap.add_argument("--model", choices=TANITA_MODELS,
                    help=f"which Tanita (with --scale tanita only); default {TANITA_MODELS[0]}, "
                         "the current one")
    ap.add_argument("--date", type=iso_date,
                    help="day of the weigh-in (YYYY-MM-DD); default today, local time")
    ap.add_argument("--approx", action="store_true",
                    help="a remembered or rounded figure, not a scale reading")
    ap.add_argument("--exclude", type=reason, metavar="REASON",
                    help="this reading is real but must be left out of means and trends, "
                         "e.g. illness (fever, not eating or drinking normally)")
    ap.add_argument("--note", default="", help="context only; the clock belongs in ts, not here")
    ap.add_argument("--agent", default=os.environ.get("AGENT_ID", UNKNOWN_AGENT),
                    help="which agent wrote this, by its estate AGENT_ID and not by its persona "
                         "name; checked against the agent directories in private-data and the "
                         "moprox-dev@ instances")
    a = ap.parse_args()
    check_agent(ap, a.agent)
    if a.model and a.scale != "tanita":
        ap.error("--model names a Tanita and needs --scale tanita; "
                 "the Salter's model is not on record")

    rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
           "date": a.date or date.today().isoformat(),
           "time": None if a.time == "unknown" else a.time,
           "kg": a.kg, "approx": a.approx, "source": "operator", "agent": a.agent,
           "scale": a.scale}
    if a.scale == "tanita":
        rec["model"] = a.model or TANITA_MODELS[0]
        rec["reference"] = True
    if a.exclude:
        rec["exclude"] = a.exclude
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
