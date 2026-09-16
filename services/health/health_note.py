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
  - Provenance is an AFFIRMATIVE act. `date_source` records where the date came from, and
    "operator" is never assumed: you get it only by passing --date-source operator. Omit the
    flag and the record says what actually happened — "clock" if you passed no --date (the
    machine clock supplied it), "unknown" if you passed one without saying where it came from.
    An inferred date that reads as reported is how a measurement becomes a circular inference;
    it has happened once already. --date-source inferred is for dates the estate derived.
  - One line per distinct fact. A cold, a sore throat and a prescription are three lines.
  - Append-only. To correct a line, add a new one and NAME the line it replaces with
    --supersedes. Saying so in prose does not count: nothing reads prose, so both halves stand.
    The retracted line stays in the file — the correction is only legible next to it — and
    --show marks it, so a reader can tell which of two disagreeing rows is the live one.

  health_note.py --kind medication --date 2026-07-30 --status ongoing "amoxicillin"
  health_note.py --kind symptom "sore throat" --verbatim "A sore throat is with me"
  health_note.py --show
  health_note.py --kind illness --date 2026-06-30 --date-source operator --status corrected \
      --supersedes 4c1b2e9a77d0 "onset was end of June; the 2026-07-19 date was estate-inferred"
"""
import argparse, hashlib, json, os, sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog

LOG = Path.home() / "projects/private-data/health/hints.jsonl"

KINDS = ["symptom", "condition", "illness", "medication", "treatment", "injury",
         "sleep", "lifestyle", "appointment", "note"]

# Where `date` came from. The distinction is not cosmetic: the 2026-07-19 cold row was dated
# from the Ultrahuman RHR step, and that date was then used to rule illness out as the cause of
# the step. Unmarked, that is invisible.
#
#   operator  they said it, or said something ("end of June") that resolves to it
#   inferred  the estate derived it from its own sensors
#   clock     no --date was passed; datetime.now() supplied it
#   unknown   a --date was passed and the writer did not say where it came from
#
# Only the first two are assertable on the command line; the last two are what the resolver
# writes when nobody asserted anything. This asymmetry is the point. The field's first version
# defaulted to "operator", which meant an unflagged caller stamped every date -- including the
# clock's own -- as operator-reported, and 13 of the log's first 16 rows are clock-dated. A
# default that manufactures provenance is worse than the absence it replaced: README.md defines
# a missing date_source as "unknown, not operator", and an affirmative "operator" defeats that.
ASSERTABLE_SOURCES = ["operator", "inferred"]
DATE_SOURCES = ASSERTABLE_SOURCES + ["clock", "unknown"]


# A row's NAME, and why it is not `ts`.
#
# This log corrects itself by appending a row that names its target IN PROSE — "CORRECTS the
# 2026-07-19 cold row", "my same-day symptom note", "today's earlier entry" — and nothing reads
# prose. Measured over the 43-row census (moprox-memory/health-hints-retraction-prose-only): 7 rows
# retract an earlier row of this same file and no field anywhere points at one. The founding
# example is still standing wrong because of it — the 2026-07-19 cold row, whose date was inferred
# from the very RHR step it was then used to explain, was re-dated in a NEW row on 2026-08-21 and
# still sits there `status: "ongoing"`, 19 days early, with nothing on it saying superseded.
#
# `status: "corrected"` is not the missing field and cannot be made into it: it marks the row DOING
# the correcting, never the row corrected, and it catches 5 of the 7 — the two it misses (the cold
# that turned out to be flu, the gastrocnemius→soleus flip) both reverse a conclusion about a
# condition that is still ongoing, so they are honestly `ongoing`. Status and retraction are
# different axes and --supersedes does not touch --status.
#
# `ts` is not a name either, and this log is a worse offender than the strength log it shares the
# shape with: 26 of its 48 live rows share a timestamp with at least one other, in ten groups, the
# widest six rows across. The log's ONE id-bearing retraction proves the point by failing at it —
# it cites `ts 2026-08-02T16:19:06`, and that second carries FOUR rows: the cold it means, plus a
# sore throat and two medications. A ts-keyed retraction there silently drops a live antibiotic
# course, and reports success.
#
# So a row is named by its own content: unique across all 48 live rows and their 8-char prefixes,
# it adds no field to any row, rewrites nothing, and names rows written long before this existed.
# Two rows that hash alike are the same record field for field, which is the one case where "which
# did you mean" has no answer — so that is refused rather than guessed.
#
# This is deliberately the same function as services/training/strength.py's `rid`, byte for byte,
# and deliberately NOT shared with it: it is three lines, and the alternative is a new import edge
# from a health CLI into a dashboard feed builder, or a lib module that changes a landed,
# twice-audited path for no behaviour. If a third append-only log grows a retraction, extract then.
def rid(r):
    """This row's identity: first 12 hex of the sha256 of its canonical JSON."""
    return hashlib.sha256(json.dumps(r, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()[:12]


def resolve(ref, rows):
    """Every row in `rows` that `ref` names — by rid, by an rid prefix, or by `ts`.

    `ts` is accepted because it is what an agent reading the log has in front of it, and on 22 of
    the 48 live rows it IS unambiguous. Ambiguity is not resolved here; it is returned, so the
    caller has to decide. Both callers refuse.

    A ref that is not a string names nothing and says so by returning nothing. TOTAL over whatever
    a JSON row can hold, because this file is hand-appendable and its producers are agents: a
    `supersedes` that arrived as a bare `true` must cost its own row, not the whole read.
    """
    if not isinstance(ref, str):
        return []
    ref = ref.strip()
    if not ref:
        return []
    return ([r for r in rows if rid(r).startswith(ref)]
            or [r for r in rows if r.get("ts") == ref])


def log_rows():
    """The log as parsed rows. A READ, not a read-modify-write: this script stays a pure append, so
    a line arriving between this read and the append can only be a row --supersedes did not name.
    A line too corrupt to parse is skipped rather than fatal — but never silently, because a
    reference that would have named it has to come out as "no such row" and not as "no rows"."""
    if not LOG.exists():
        return []
    rows = []
    for line in LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as e:
            errlog.err(f"health_note: unparseable line in {LOG}, not searchable by --supersedes", e)
    return rows


def retracted_by(rows):
    """{rid of a retracted row: rid of the row retracting it}.

    A ref that names no row, or more than one, retracts NOTHING and reaches the journal at err.
    Guessing is the failure this whole field exists to avoid: a reader that drops four rows when it
    was told one has turned a correction into data loss with no outward sign, and the log's own
    founding retraction is exactly that shape. Refusing leaves the two rows disagreeing in the
    open, which is the state this log was already in — loudly, and where a reader can see both.
    """
    out = {}
    for r in rows:
        ref = r.get("supersedes")
        if ref is None:
            continue
        if not isinstance(ref, str):
            errlog.err("health_note: supersedes is %s, not a row id, so nothing was retracted"
                       % type(ref).__name__,
                       ValueError(json.dumps(r, ensure_ascii=False)[:200]))
            continue
        hit = [t for t in resolve(ref, rows) if rid(t) != rid(r)]
        if len(hit) != 1:
            errlog.err("health_note: supersedes %r names %d rows, so nothing was retracted"
                       % (ref, len(hit)), ValueError(json.dumps(r, ensure_ascii=False)[:200]))
            continue
        out[rid(hit[0])] = rid(r)
    return out


def show():
    """Print the log as JSONL, every row carrying its `rid`, retracted rows carrying `superseded_by`.

    NOTHING is dropped. The strength feed drops a retracted row because it is publishing a total;
    this file is read for context, and a correction is only legible next to what it corrects — so
    the fix here is to LABEL, not to hide. It is also the only place a row's id is discoverable
    for the 48 rows appended before ids existed.
    """
    rows = log_rows()
    gone = retracted_by(rows)
    for r in rows:
        out = dict(r)
        out["rid"] = rid(r)
        if out["rid"] in gone:
            out["superseded_by"] = gone[out["rid"]]
        print(json.dumps(out, ensure_ascii=False))
    return 0


def resolve_date(explicit_date, explicit_source):
    """(date, date_source) -- provenance follows the date, and is never assumed to be the operator."""
    if explicit_date is None:
        return date.today().isoformat(), explicit_source or "clock"
    return explicit_date, explicit_source or "unknown"


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
    ap.add_argument("text", nargs="?", help="the observation, in plain words")
    ap.add_argument("--kind", choices=KINDS, default="note")
    ap.add_argument("--date", type=iso_date,
                    help="date the thing STARTED or applies to (YYYY-MM-DD); "
                         "default today. Resolve 'Thursday' yourself.")
    ap.add_argument("--date-source", choices=ASSERTABLE_SOURCES, default=None,
                    help="'operator' if they stated the date, 'inferred' if the estate derived "
                         "it from its own data. NOT optional decoration: omit it and the record "
                         "says 'clock' or 'unknown' rather than crediting the operator.")
    ap.add_argument("--status", default="ongoing",
                    choices=["onset", "ongoing", "resolved", "corrected", "unknown"])
    ap.add_argument("--verbatim", help="operator's own words, when the phrasing carries nuance")
    # The vocabulary for "that earlier row was wrong". Without it the only way to retract is prose,
    # and prose is what nothing reads — see rid() above for the seven live cases and the one that
    # is still standing wrong. It takes a row's id, not its timestamp, and it RESOLVES the
    # reference here rather than storing what was typed, so an append-only file only ever carries
    # an unambiguous name even when the caller gave an ambiguous one.
    ap.add_argument("--supersedes", metavar="ROW",
                    help="retract the earlier row this one replaces, by its id (printed by this "
                         "script on every append, and by --show) or by its timestamp if that "
                         "names exactly one row. The retracted row stays in the log.")
    ap.add_argument("--show", action="store_true",
                    help="print the log as JSONL with each row's id, and `superseded_by` on rows "
                         "a later row has retracted. Writes nothing.")
    ap.add_argument("--agent", default=os.environ.get("AGENT_ID", "unknown"))
    a = ap.parse_args()

    if a.show:
        return show()
    if a.text is None:
        ap.error("give the observation as text, or --show to read the log")

    # Resolved BEFORE the append, and refused rather than guessed at. This is the one place in the
    # estate that reads this file with code, so it is the only place a wrong retraction can be
    # stopped: the file is append-only, and a bad ref written into it cannot be taken back out.
    # Refusing hands the caller the ids, which is also how ids are discoverable for a row whose
    # timestamp names four.
    sup = None
    if a.supersedes is not None:
        hit = resolve(a.supersedes, log_rows())
        if not hit:
            ap.error("--supersedes %s names no row in %s" % (a.supersedes, LOG))
        if len(hit) > 1:
            ap.error("--supersedes %s names %d rows — give one of these ids instead:\n%s"
                     % (a.supersedes, len(hit),
                        "\n".join("  %s  %s  %-11s %s" % (rid(h), h.get("date", "?"),
                                                          h.get("kind", "?"),
                                                          (h.get("text") or "")[:60])
                                  for h in hit)))
        sup = rid(hit[0])

    d, src = resolve_date(a.date, a.date_source)
    rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
           "date": d, "date_source": src,
           "agent": a.agent, "kind": a.kind, "status": a.status, "text": a.text}
    if a.verbatim:
        rec["verbatim"] = a.verbatim
    if sup:
        rec["supersedes"] = sup

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False))
    # The id is printed on EVERY append, not only a retracting one, because the row that will need
    # retracting is never the row you expected to. This is the only moment the name is free.
    print("id %s%s" % (rid(rec),
                       "  (supersedes %s)" % sup if sup else "  — pass to --supersedes to retract"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
