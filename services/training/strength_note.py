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

A session whose set count was never tracked says so, rather than losing the load with it:

  strength_note.py --ex heel-raise-SL-loaded --sets-unknown --reps 10 --kg 32

A row that was WRONG is retracted by appending its replacement and naming it. The file still keeps
both; the feed counts only the later one:

  strength_note.py --ex side-plank --sets 2 --secs 30 --supersedes 9c10c7f80c9b
"""
import argparse, json, os, sys, time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import errlog
# The reader, imported for `rid`/`resolve` alone. Row identity has to be ONE function: a writer that
# names rows its own way and a reader that resolves them another way agree until the day they do
# not, and the disagreement shows up as a retraction that deletes the wrong row.
import strength

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


def log_rows():
    """The log as parsed rows. A READ, not a read-modify-write: this script stays a pure append, so
    a line arriving between this read and the append below can only be a row --supersedes did not
    name. A line too corrupt to parse is skipped rather than fatal, for the same reason the reader
    skips it — but it is never silent, because a reference that would have named it must come out
    as "no such row" and not as "no rows at all"."""
    if not LOG.exists():
        return []
    rows = []
    for line in LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as e:
            errlog.err(f"strength_note: unparseable line in {LOG}, not searchable by --supersedes",
                       e)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="Append one movement to the strength log.")
    ap.add_argument("--ex", required=True, help="movement, lowercase-hyphenated (seated-row, goblet-squat)")
    ap.add_argument("--sets", type=int)
    # Not "--sets optional". Forgetting a set count and declaring one unknown are different acts,
    # and only one of them should be possible by accident — so --sets stays mandatory unless this
    # is passed, and passing both is an error. The need is real and already on disk: four rows in
    # the live log carry a load and no set count, every one of them appended as hand-written JSON
    # that went around this script because this script would not take them, one of them carrying
    # an invented `sets_uncertain: true` that no reader has ever looked at. A producer reaching for
    # a word the sanctioned writer does not have writes raw JSON instead, and then the vocabulary
    # is whatever it improvised. strength.py reads the absence of `sets`, which is what this emits.
    ap.add_argument("--sets-unknown", action="store_true",
                    help="the set count was not tracked (an at-home session, a movement named "
                         "after the fact). Volume load is then not computed for this row and the "
                         "session's set total is published as a floor.")
    ap.add_argument("--reps", type=int, help="reps per set (omit for a timed movement)")
    ap.add_argument("--kg", type=float, help="load per set; omit for bodyweight")
    ap.add_argument("--kg-src", choices=KG_SRC,
                    help="where --kg came from: 'stated' (read off the machine) or 'assumed' "
                         "(the prescription, not read back). Omit if you do not know.")
    ap.add_argument("--secs", type=int, help="seconds per set, for timed movements (plank etc)")
    ap.add_argument("--rir", type=float, help="reps in reserve — how many were left. 0 = to failure")
    # The vocabulary for "that earlier row was wrong". Without it the only way to retract is prose,
    # and prose is what nothing reads: three live cases where a correction was appended, said so in
    # its note, and the panel counted both halves — one lift under two movement keys each
    # publishing 32 kg, a session set total of 13 for 10 sets performed.
    #
    # It takes a row's id, not its timestamp, and it RESOLVES the reference here rather than
    # storing whatever was typed, so the file carries an unambiguous name even when the caller
    # gave an ambiguous one. See strength.rid for why a timestamp is not a name: today's log has
    # the retracted side-plank and the heaviest lift in the corpus stamped in the same second.
    ap.add_argument("--supersedes", metavar="ROW",
                    help="retract the earlier row this one replaces, by its id (printed by this "
                         "script on every append) or by its timestamp if that names exactly one "
                         "row. The retracted row stays in the log and leaves the feed.")
    ap.add_argument("--date", help="YYYY-MM-DD; default today. Resolve 'Friday' yourself.")
    ap.add_argument("--note", default="")
    ap.add_argument("--agent", default=os.environ.get("AGENT_ID", ""))
    a = ap.parse_args(argv)

    if (a.sets is None) != a.sets_unknown:
        ap.error("give --sets N, or --sets-unknown if it was not tracked — not both, not neither")
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

    # Resolved BEFORE the append, and refused rather than guessed at. An ambiguous reference is the
    # one thing this field must never absorb: the row it does not name is a real set of work, and
    # deleting it looks exactly like the retraction succeeding. So the caller is handed the ids and
    # made to choose — which is also the only place the ids are discoverable for rows appended
    # before they were printed.
    sup = None
    if a.supersedes is not None:
        hit = strength.resolve(a.supersedes, log_rows())
        if not hit:
            ap.error("--supersedes %s names no row in %s" % (a.supersedes, LOG))
        if len(hit) > 1:
            ap.error("--supersedes %s names %d rows — give one of these ids instead:\n%s"
                     % (a.supersedes, len(hit),
                        "\n".join("  %s  %s  %-34s %s" % (strength.rid(h), h.get("date", "?"),
                                                          h.get("ex", "?"),
                                                          (h.get("note") or "")[:50])
                                  for h in hit)))
        sup = strength.rid(hit[0])

    rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
           "date": a.date or time.strftime("%Y-%m-%d"),
           "ex": a.ex.strip().lower().replace(" ", "-")}
    for k, v in (("sets", a.sets), ("reps", a.reps), ("kg", a.kg), ("kg_src", a.kg_src),
                 ("secs", a.secs), ("supersedes", sup),
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
    # The id is printed on EVERY append, not only when one was retracted, because the row that will
    # need retracting is never the row you expected to. It is the only moment the name is free:
    # afterwards it has to be recovered by resolving an ambiguous timestamp.
    print("id %s%s" % (strength.rid(rec),
                       "  (supersedes %s)" % sup if sup else "  — pass to --supersedes to retract"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
