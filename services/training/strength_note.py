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

An accessory done against a RUBBER BAND has no kg; the colour is the resistance, and the log asks
for it every session because progression is only trackable across sessions that name it:

  strength_note.py --ex band-foot-inversion --sets 3 --reps 15 --band yellow

A session whose set count was never tracked says so, rather than losing the load with it:

  strength_note.py --ex heel-raise-SL-loaded --sets-unknown --reps 10 --kg 32

An UNEVEN session — three sets of 10, 10, 8 against a 3x10 target — gives one count per set:

  strength_note.py --ex db-chest-press --sets 3 --reps 10,10,8 --kg 20

A row that was WRONG is retracted by appending its replacement and naming it. The file still keeps
both; the feed counts only the later one:

  strength_note.py --ex side-plank --sets 2 --secs 30 --supersedes 9c10c7f80c9b

A row whose replacement was already written — or that simply did not happen — is retracted on its
own, with no shape of its own. The movement and the session date are taken from the row it
withdraws, so there is no new word to invent for either:

  strength_note.py --supersedes 1e0035d7d4b4

A row that is PROSE and not work — a session remark, a caveat addressed to whoever reads the Polar
recording, a disclosure — says so and carries no dose. The reader counts it as a note, never an
entry:

  strength_note.py --ex SESSION-END --note-only --note "right hamstring quiet throughout"

Prose cannot retract: nothing downstream reads it. A correction names the row it replaces, with
--supersedes above, and an `ex` that claims to correct is refused here.

A movement the log has NEVER SEEN is refused unless you say it is new. `ex` is matched exactly
across sessions, so a shorter spelling of an existing lift is a different movement and its panel
stops. The refusal lists the keys that exist; a genuinely new movement repeats with --new-ex:

  strength_note.py --ex nordic-curl --new-ex --sets 2 --reps 5

The id to pass it is printed on every append, and by --show, which is how you obtain one for a row
written earlier:

  strength_note.py --show
"""
import argparse, difflib, json, os, re, sys, time
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


# A prose row's `ex` is a MARKER, not a movement, and it must not be a claim that some other row is
# wrong. That claim has a field (`supersedes`), and when it is made in prose instead, nothing
# downstream reads it: moprox-memory/strength-log-correction-row-type-unresolved measured 6 rows of
# `ex: "CORRECTION"`, every one appended after `supersedes` shipped, 5 of them naming content the
# live feed went on publishing uncorrected — four of those a published MEASURE, including an OHP
# load published as 27.5 kg beside the same artifact's own note reading "21 Oct 28 x 9/9/8".
#
# The stems are that fact's own correction-marker list (supersedes|CORRECTS|CLARIFIES|RESOLVES|
# AMENDMENT|CORRECTION|REPLACES), so the writer's rule and the census that found the rows agree on
# one vocabulary. `COMPLETES` is deliberately NOT here: "complete" is a plausible spelling of a
# legitimate session marker (the log's own is SESSION-END), and a false refusal sends the row back
# to the hand-written path this flag exists to close. Checked against every `ex` in the live log
# 2026-11-24: three values match, and all three are the defect — `CORRECTION`,
# `heel-raise-order-correction`, `calf-raise-leg-press-LOAD-CORRECTION`.
#
# Only the `ex`, never the `note`. Prose is allowed to EXPLAIN a correction — that is what the
# replacement row's note is for. What it may not do is be the correction.
CORRECTION_MARKER = re.compile(r"supersed|correct|clarif|resolv|amend|replace", re.I)


# One count per set, because that is how an uneven session is actually done — and because the log
# has carried that shape since 2026-09-21 while this writer could not produce it.
#
# All 6 rows in the live log whose `reps` is a list (`[10,10,8]`, `[10,12,15]`, `[10,10,6]`,
# `[10,10,8]`, `[10,10,7]`, `[10,10,7.5]`, newest 2026-10-02, every one `agent: coach`) are
# hand-written JSON that went around this script, because `--reps` took one integer and a session
# of 10/10/8 is not one integer. The cost of that is measured and it is the whole feed: `float()`
# of a list raised out of strength.volume(), strength.py died on every run from 2026-09-21, and
# update.py's non-fatal wrapper went on serving the previous strength.json — 26 failed runs, the
# panel frozen for 9 days, with no outward sign — until b7dd0271 taught the READER the shape.
#
# Teaching the reader a shape the writer cannot write fixes that row and not the next one: the row
# still arrives by hand, past every check in this file, so the vocabulary it uses is whatever it
# improvised. The live log is the evidence — `sets_uncertain` and `date_uncertain`, invented on
# hand-written rows by this same producer and read by nothing to this day. The durable fix is for
# the sanctioned writer to have the word.
#
# Floats are accepted because the log already holds one: 2026-10-02's overhead press is 10/10/7.5,
# "the last rep barely". Rejecting it would send that row back around the CLI.
def reps_arg(s):
    """`10` or `10,10,8` -> [10] / [10, 10, 8]. One entry per set, in the order performed."""
    out = []
    for part in str(s).split(","):
        part = part.strip()
        if not part:
            raise argparse.ArgumentTypeError("empty rep count in %r — give 10 or 10,10,8" % (s,))
        try:
            n = float(part)
        except ValueError:
            raise argparse.ArgumentTypeError("%r is not a rep count" % (part,))
        if n <= 0:
            raise argparse.ArgumentTypeError("a set of %s reps is not a set" % (part,))
        out.append(int(n) if n == int(n) else n)
    return out


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


# Which movement keys EXIST, and what to offer when the one typed is not among them.
#
# `--ex` is free text and the reader matches it exactly (strength.PROGRESS, and every movement's
# history), so a near-miss is not an error anywhere downstream: it is a new movement with one row.
# Measured (i-20261124-192305): between 9 and 18 Nov 2026 `row`, `leg-curl` and `calf-raise` were
# typed for `seated-row`, `leg-curl-technogym-isotonic` and `standing-barbell-calf-raise`. Six rows
# went in at rc=0 with nothing on stderr, three progression panels stopped at 6 Nov for two and a
# half weeks, and `skipped` stayed 0. The prose rule against inventing names was already in the
# coach's instructions and did not prevent it.
#
# Deliberately NOT `choices=` and not an alias table: the September rehab block coined thirteen
# one-row movements legitimately, and a closed vocabulary would send those back to hand-written
# JSON. --new-ex keeps them on the CLI, as a declared act and not an accident.
#
# Known = the `ex` of every LIVE row carrying a dose, plus the PROGRESS keys (so an empty log still
# knows the weights day). Retracted rows are dropped, so a re-filed alias stops being known; prose
# rows are dropped, so a marker (SESSION-END) never becomes a movement.
#
# A DOSE is wider than strength.MEASURES, and the gate's own suggestion list is why it has to be.
# `sets_detail` is one because progress() reads those rows as work. The three CARDIO keys are the
# others: `treadmill-walk` has two live rows (2026-09-08 and 09-10, `mins`/`kph`/`grade_pct`, the
# second comparing its grade to the first) and not one strength.MEASURES field between them, so on
# MEASURES alone the log's own movement is invisible to BOTH halves of this gate — the membership
# test refuses `--ex treadmill-walk` at rc=2 as "not a movement this log has seen", and
# candidates() cannot offer a key `known` does not hold, so a near-miss (`--ex treadmill-walking`)
# is refused with "(nothing close; --show lists every row)". The refusal's own remedy is then
# --new-ex, i.e. a fifth alias — the gate pointed backwards at the thing it exists to stop.
# The three keys occur on those two rows and on no other row in the log, so no prose marker and no
# bare retraction is admitted with them.
DOSE = strength.MEASURES + ("sets_detail", "mins", "kph", "grade_pct")


def known_ex(rows):
    """{ex: [live dose-row count, last date]} over the log, plus strength.PROGRESS keys at 0."""
    gone = strength.superseded(rows)
    known = {}
    for r in rows:
        ex = r.get("ex")
        if not isinstance(ex, str) or not ex.strip() or strength.rid(r) in gone:
            continue
        if all(r.get(k) is None for k in DOSE):
            continue
        n = known.setdefault(ex, [0, ""])
        n[0] += 1
        n[1] = max(n[1], str(r.get("date") or ""))
    for p in strength.PROGRESS:
        known.setdefault(p[0], [0, ""])
    return known


def candidates(name, known, limit=8):
    """Known keys the caller most plausibly meant by `name`, best first.

    Hyphen-token overlap FIRST, then difflib. difflib alone does not find `seated-row` from `row`
    (ratio 0.46, under any cutoff that keeps noise out), and that is the exact miss this exists
    for, so the token match is not optional."""
    toks = set(name.lower().split("-")) - {""}
    shared = [(len(toks & set(k.lower().split("-"))), known[k][0], k) for k in known]
    out = [k for n, _, k in sorted((s for s in shared if s[0]), key=lambda s: (-s[0], -s[1], s[2]))]
    low = {}
    for k in known:
        low.setdefault(k.lower(), k)
    for m in difflib.get_close_matches(name.lower(), list(low), n=limit, cutoff=0.6):
        if low[m] not in out:
            out.append(low[m])
    return out[:limit]


# How a row written earlier is NAMED, and why reading the log is not enough to do it.
#
# --supersedes takes a content id. The append prints one, which covers the row you have just
# written and nothing else — and the row that needs retracting is never that row. For anything
# older the only name left is `ts`, and on the live log (166 rows at 2026-10-18) 45 of them, 27.1%,
# share a timestamp with another row, in 19 groups, the widest SEVEN across — and the correction
# this field was built for is in that seven. resolve() returns all of them and every caller refuses
# rather than guess, correctly, so those 45 rows could not be retracted at all; `supersedes` occurs
# 0 times in the file a month after it shipped. The one id-discovery path that existed was to
# compose a doomed append against an ambiguous timestamp and read the ids out of the error, which
# is documented nowhere and works ONLY for the ambiguous case: a row whose `ts` is unique resolves
# and the append happens, so there was no way to look at a reference before committing it to an
# append-only file.
#
# This LABELS and never drops, the same way health_note.py --show does and for the same reason: the
# feed drops a retracted row because it is publishing a total, but this output is read to find out
# what the file says, and a correction is only legible next to what it corrects.
def show():
    """Print the log as JSONL, every row carrying its `rid`, retracted rows their `superseded_by`."""
    rows = log_rows()
    gone = strength.superseded(rows)
    for r in rows:
        out = dict(r)
        # rid of the ROW, not of this annotated copy: the id has to be the one --supersedes will
        # resolve, and a `rid` key inside the hash would make every printed id name nothing.
        out["rid"] = strength.rid(r)
        if out["rid"] in gone:
            out["superseded_by"] = gone[out["rid"]]
        print(json.dumps(out, ensure_ascii=False))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Append one movement to the strength log.")
    # Not `required=True` any more, because --show writes nothing and has no movement. Absence is
    # still an error for every other invocation, checked below: forgetting --ex must not become a
    # way to read the log.
    ap.add_argument("--ex", help="movement, lowercase-hyphenated (seated-row, goblet-squat). Must "
                                 "be a key the log already has, unless --new-ex is passed.")
    ap.add_argument("--new-ex", action="store_true",
                    help="--ex is a movement the log has never seen, and that is intended. Without "
                         "this an unknown --ex is refused and the existing keys are listed.")
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
    ap.add_argument("--reps", type=reps_arg,
                    help="reps per set (omit for a timed movement). One count for an even "
                         "session, or one per set for an uneven one: 10,10,8")
    ap.add_argument("--kg", type=float, help="load per set; omit for bodyweight")
    ap.add_argument("--kg-src", choices=KG_SRC,
                    help="where --kg came from: 'stated' (read off the machine) or 'assumed' "
                         "(the prescription, not read back). Omit if you do not know.")
    ap.add_argument("--secs", type=int, help="seconds per set, for timed movements (plank etc)")
    # The load on an accessory done against a rubber band, where the colour IS the resistance —
    # there is no kg to read. The log states its own measurement rule on 2026-09-25,
    # "Measure = band colour + setup + reps to the burn", and asks for the colour every time
    # (2026-09-15: "Record the colour each time so resistance progression is trackable").
    #
    # strength.py has published the key since 3a0f5e27 and index.html renders it, but this word has
    # never existed here: `band` occurs 0 times in this file and `git log -S band` over it returns
    # no commit, ever (moprox-memory/strength-key-four-drop-points-band-writer-open, measured
    # 2026-11-22). So all 5 live band rows were necessarily hand-written JSON that went around this
    # script — which is the mechanism the --sets-unknown comment above blames by name, and the
    # reason that same producer has `sets_uncertain` and `date_uncertain` on disk, invented words
    # no reader has ever looked at.
    #
    # NOT `choices=`, unlike --kg-src. That one is a closed set; this one is not: the log says
    # outright that "colour-to-tension mapping differs by brand, so it is only a relative marker",
    # and a vocabulary fixed to today's green/yellow would send the next band's colour straight
    # back around the CLI — the exact failure this flag exists to end. Normalised like --ex instead,
    # because it is the same kind of value: a word a human typed, which the panel then matches
    # across sessions.
    #
    # And deliberately NOT a movement shape: it does not satisfy the --reps/--secs gate below and
    # does not count as a measurement for a bare retraction. strength.py leaves `band` out of
    # MEASURES on purpose — the 2026-09-29 row is "Unmeasured by agreement ... band colour logged
    # only", so the colour alone does not make a row an entry — and a writer that accepted it as a
    # shape would hand the reader rows it classifies the other way. So this emits the colour onto a
    # row that has a dose; it does not invent a fourth shape.
    ap.add_argument("--band", metavar="COLOUR",
                    help="resistance band colour, for an accessory done against a band rather "
                         "than a weight (green, yellow). The colour ALONE — the panel appends the "
                         "word 'band' itself. Not a dose: give --reps with it.")
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
                         "script on every append, and by --show) or by its timestamp if that names "
                         "exactly one row. The retracted row stays in the log and leaves the feed.")
    ap.add_argument("--show", action="store_true",
                    help="print the log as JSONL with each row's id, and `superseded_by` on rows "
                         "a later row has retracted. Writes nothing.")
    # PROSE, as a shape of its own. The reader has always classified a row carrying none of
    # strength.MEASURES as a `note` rather than an entry, and publishes the count beside `skipped`
    # — "free-standing session prose, attached to a movement name or to a marker of its own" — but
    # this writer has never been able to write one, because the two shape gates below are
    # unconditional for anything that is not a bare retraction.
    #
    # Measured on the live log 2026-11-24: 264 rows, 51 of them notes, and 40 are pure prose
    # (ts/date/ex/note/agent and nothing else), every one `agent: coach`, 2026-09-08..2026-11-23.
    # Only the 3 carrying `supersedes` were writable here. So 40 rows necessarily went around this
    # script, which is the mechanism the --sets-unknown and --band comments above blame by name,
    # and the invented vocabulary it produced is on disk beside them: `sets_detail`, `rest_s`,
    # `exercise_uncertain`, and 6 rows of `ex: "CORRECTION"` that correct nothing a reader sees.
    #
    # Declared, not inferred from the absence of a dose: forgetting --reps must stay an error, for
    # the reason --sets-unknown exists at all. Only one of "no dose" and "a dose I forgot to type"
    # should be reachable by accident.
    ap.add_argument("--note-only", action="store_true",
                    help="this row is PROSE, not work: a session remark, a caveat, a disclosure. "
                         "Needs --note and takes no dose flag. The reader counts it as a note, "
                         "never an entry. A CORRECTION is not prose — use --supersedes.")
    ap.add_argument("--date", help="YYYY-MM-DD; default today. Resolve 'Friday' yourself.")
    ap.add_argument("--note", default="")
    ap.add_argument("--agent", default=os.environ.get("AGENT_ID", ""))
    a = ap.parse_args(argv)

    if a.show:
        return show()
    # A RETRACTION ON ITS OWN: --supersedes and not one measurement. The three shape gates below do
    # not apply to it, because it HAS no shape — it asserts only that the named row did not happen
    # as written. Until now they were unconditional, so the sanctioned writer could not express it:
    # the cheapest form they accept writes `reps: 1`, which is an entry, and the reader counts it.
    #
    # That is not a hypothetical. The three double-counts of
    # moprox-memory/strength-log-supersession-unread are the reason `supersedes` exists, and they
    # were still live 66 days after it shipped (i-20261120-141753) because their replacements —
    # rows 35, 42 and 46 — were appended in September, so the "append the replacement and name it"
    # form above cannot reach them. Retracting them needs a row that names and nothing else.
    #
    # The reader already honours one: a row carrying none of strength.MEASURES is a `note`, never
    # an entry (strength.entries), and `supersedes` is resolved over the whole file BEFORE that
    # classification, so a note retracts exactly as an entry does. Nothing downstream can assume
    # every row has a shape either — 47 of the live log's 249 rows are already such notes.
    retract_only = a.supersedes is not None and not any(
        (a.sets is not None, a.sets_unknown, a.reps is not None, a.secs is not None,
         a.kg is not None, a.rir is not None))
    if a.ex is None and not retract_only:
        ap.error("give --ex MOVEMENT, or --show to read the log")
    # Declared prose: the shape gates do not apply, because this row HAS no shape. What does apply
    # is that it must say something, that it must not carry half a dose, and that it must not be a
    # correction. A `--note-only --supersedes <id>` row is a prose replacement for a prose row and
    # is fine: it names what it withdraws, which is the whole difference.
    if a.note_only:
        dose = [n for n, given in (("--sets", a.sets is not None), ("--sets-unknown", a.sets_unknown),
                                   ("--reps", a.reps is not None), ("--secs", a.secs is not None),
                                   ("--kg", a.kg is not None), ("--kg-src", a.kg_src is not None),
                                   ("--rir", a.rir is not None), ("--band", a.band is not None))
                if given]
        if dose:
            # --band among them on purpose: a colour with no dose is a note to the reader, so the
            # panel never renders it, and this flag must not become the way to write one.
            ap.error("--note-only is a row with no dose; drop %s, or drop --note-only"
                     % ", ".join(dose))
        if not a.note.strip():
            ap.error("--note-only needs --note: the prose IS the row")
        if a.ex is not None and CORRECTION_MARKER.search(a.ex):
            # Refused on what the CALLER typed, not on an `ex` inherited from --supersedes: that
            # value is already in the log, and a retraction of a CORRECTION row has to be able to
            # name it.
            ap.error("--ex %s claims an earlier row is wrong, and nothing downstream reads prose: "
                     "append the corrected row with --supersedes <id>, or retract the wrong row on "
                     "its own with --supersedes <id>. Ids: --show." % a.ex)
    if not retract_only and not a.note_only:
        if (a.sets is None) != a.sets_unknown:
            ap.error("give --sets N, or --sets-unknown if it was not tracked — not both, not neither")
        if a.reps is None and a.secs is None:
            ap.error("give --reps (weighted or bodyweight) or --secs (timed)")
    if a.reps is not None and a.secs is not None:
        ap.error("--reps and --secs are different movement shapes; give one")
    # A per-set list STATES the set count, so it must not contradict the one the caller also gave.
    # Refused rather than reconciled: one row answering "how many sets" two ways is the ambiguity
    # the hand-written path has no opinion about, and picking a winner here would publish a set
    # total and a volume load computed off different session shapes. All 6 live list rows agree
    # with their own `sets`, so nothing legitimate is being turned away.
    if a.reps is not None and len(a.reps) > 1:
        if a.sets is not None and a.sets != len(a.reps):
            ap.error("--reps lists %d sets (%s) but --sets says %d"
                     % (len(a.reps), ",".join(str(n) for n in a.reps), a.sets))
        if a.sets_unknown:
            ap.error("--reps %s states %d sets, so the count is not unknown"
                     % (",".join(str(n) for n in a.reps), len(a.reps)))
    # `kg: 0` is how this log spells "no external load", not a load of zero, and strength.py's
    # load() reads it that way — so it attributes no source for either spelling and would drop the
    # field. Rejecting here is the difference between the writer knowing that and believing it
    # recorded a provenance it did not.
    if a.kg_src and not a.kg:
        ap.error("--kg-src describes --kg; give a non-zero --kg or drop it")
    # An empty colour is a caller error, not an omission, and it has to be LOUD. Dropping it the way
    # --note's default is dropped would append the row with no `band` key at all, i.e. publish a
    # banded accessory as though the band were never used — and on these rows the colour is the only
    # load there is, so the quiet version loses the entire measurement.
    band = None if a.band is None else a.band.strip().lower()
    if a.band is not None and not band:
        ap.error("--band takes the band's colour (green, yellow); give one or drop the flag")

    # Resolved BEFORE the append, and refused rather than guessed at. An ambiguous reference is the
    # one thing this field must never absorb: the row it does not name is a real set of work, and
    # deleting it looks exactly like the retraction succeeding. So the caller is handed the ids and
    # made to choose — which is also the only place the ids are discoverable for rows appended
    # before they were printed.
    # Read ONCE, for both checks that need the log: an unparseable line is reported by log_rows()
    # and must not be reported twice for one append.
    gated = not a.note_only and not retract_only
    rows = log_rows() if (a.supersedes is not None or gated) else []
    sup, ex = None, None
    if a.supersedes is not None:
        hit = strength.resolve(a.supersedes, rows)
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
        # A retraction's movement and session date are the withdrawn row's, taken from it rather
        # than retyped. That is the whole vocabulary question answered: a sentinel `ex` ("retraction")
        # would be a word no reader knows, and a `date` of today would plant a note on a day with no
        # session — this backlog is September rehab work. Both still yield to an explicit flag, and
        # neither is inherited for a REPLACEMENT row: the live corrections change the movement key
        # (14 Sep's `heel-raise-SL-loaded` was replaced by `heel-raise-bent-knee-SL-loaded`), so
        # inheriting there would quietly file the new work under the old name.
        if retract_only:
            if a.ex is None:
                # VERBATIM, not through the normaliser below: that one is for what a human typed,
                # and this value was already written. `heel-raise-straight-knee-SL-loaded`
                # lowercased is a movement name the corpus does not use, so normalising an
                # inherited key would make the retraction name a near-miss of the row it withdraws.
                ex = hit[0].get("ex")
                if not isinstance(ex, str) or not ex.strip():
                    ap.error("--supersedes %s names a row with no movement to inherit — give --ex"
                             % a.supersedes)
            if a.date is None and isinstance(hit[0].get("date"), str):
                a.date = hit[0]["date"]

    # A prose MARKER keeps its case; a movement key does not. Lowercasing is there because `ex` is
    # matched across sessions to build a movement's history, and a marker is matched across
    # nothing: notes are classified out before any movement key exists. The live log's markers are
    # upper (21 `SESSION-END`, 3 `HR-TRACE-CAVEAT`), so lowercasing one here would put a second
    # spelling of the log's own vocabulary on disk — which is what this flag exists to stop.
    # Whitespace still folds to hyphens: `ex` is one token either way.
    name = None if a.ex is None else a.ex.strip().replace(" ", "-")
    # THE UNKNOWN-MOVEMENT GATE (see known_ex). A refusal BEFORE the append, not a warning after
    # it: a stderr line beside rc=0 is what an agent does not read, and by then the row is in an
    # append-only file. Only a dose row is asked — prose markers are not movements, and a bare
    # retraction inherited its `ex` verbatim from a row already on disk.
    if gated:
        known = known_ex(rows)
        name = name.lower()
        if name not in known:
            # Eight live keys carry capitals (`heel-raise-bent-knee-SL-loaded`) and this writer
            # lowercases what is typed, so without this the refusal would name a key the caller
            # cannot write. A case-only match to exactly one known key IS that key, and is written
            # in the spelling the log already has. Two spellings differing only in case: refuse,
            # do not pick.
            same = [k for k in known if k.lower() == name]
            if len(same) == 1:
                name = same[0]
            elif not a.new_ex:
                cand = same or candidates(name, known)
                listing = "\n".join(
                    "  %-38s %s" % (k, "%d row%s, last %s" % (known[k][0], "s" * (known[k][0] != 1),
                                                              known[k][1] or "?")
                                    if known[k][0] else "no rows yet (a dashboard panel key)")
                    for k in cand) or "  (nothing close; --show lists every row)"
                ap.error("--ex %s is not a movement this log has seen. Closest existing keys:\n%s\n"
                         "if it is one of these, use that key; if it is a new movement, repeat "
                         "with --new-ex" % (name, listing))
    rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
           "date": a.date or time.strftime("%Y-%m-%d"),
           # `name` for a dose row is already lowercased, or a known key in the log's own spelling.
           "ex": ex if ex is not None else (name if (a.note_only or gated) else name.lower())}
    # An even session stays a bare number, exactly as every row written before this did. Emitting
    # [10] for `--reps 10` would change the shape — and therefore the content id — of every row
    # this writer appends from now on, for no gain: strength.per_set() reads a list, and a list of
    # one says nothing a number does not.
    reps = None if a.reps is None else (a.reps[0] if len(a.reps) == 1 else a.reps)
    for k, v in (("sets", a.sets), ("reps", reps), ("kg", a.kg), ("kg_src", a.kg_src),
                 ("band", band), ("secs", a.secs), ("supersedes", sup),
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
