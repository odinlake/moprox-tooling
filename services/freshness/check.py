#!/usr/bin/env python3
"""check.py — is each data lane fresh, and is what arrived any good?

The incident queue is fed by systemd unit failures, which only ever sees units that FAIL. This
covers the other class: the unit exits 0, the timer is green, and the data is quietly wrong. A
breach here logs a journal record carrying the sink's MSGID_LANE_STALE, so it aggregates into the
same queue as a unit failure — one incident per lane, deduped by UNIT=lane-<name> — with no changes
needed on the sink side.

Exit status is 0 even when lanes are stale: the breach IS the output, and failing the unit as well
would raise a second, duplicate incident for the checker itself. A non-zero exit here means the
checker is broken, which is a different thing and should look different.

    check.py            evaluate and log breaches
    check.py --dry      evaluate and print; log nothing
"""
import glob as globmod
import json, os, re, socket, subprocess, sys, time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog

LANES = Path(__file__).resolve().parent / "lanes.json"
MSGID_LANE_STALE = "6d0a9d7d5f1c4a3b8e2c74f0a1b93e55"     # must match logview/server.py
HOUR = 3600.0

# Estate services sit on 10.10.10.0/24, which `no_proxy` names in CIDR form. curl parses that and
# goes direct; urllib.request.proxy_bypass does NOT parse CIDR, so a bare urlopen to an estate IP
# is routed into the proxy at 10.10.10.2:3128 and answered 403 — which reads exactly like an auth
# failure or an outage and would make this checker cry stale about a healthy lane. Same idiom as
# services/metrics/collect.py. See moprox-memory/localnews-lane-stalled-and-unwatched.md.
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def expand(pattern):
    return sorted(globmod.glob(os.path.expanduser(pattern)))


def parse_ts(v):
    """Accept ISO8601 (with or without offset), YYYY-MM-DD, or an epoch number. None if unusable."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v if v < 1e11 else v / 1000.0)
    s = str(v).strip()
    try:
        d = datetime.fromisoformat(s)
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return None


def read_jsonl(paths, skips):
    for p in paths:
        try:
            with open(p) as f:
                for i, ln in enumerate(f, 1):
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        yield json.loads(ln)
                    except Exception as exc:
                        # `Skips` has no .skip() — that is errlog's module-level one-liner, and this
                        # was the only site in the repo calling it on the instance. The resulting
                        # AttributeError was raised INSIDE this handler, so the outer except below
                        # caught it, reported the false "cannot read <file>", and abandoned the rest
                        # of the file: one truncated append hid every record after it. Measured at
                        # 5b3598a on a 3-line fixture (old row, bad line, row dated now) —
                        # check_jsonl_newest returned "newest record is 58718.6 h old" for a lane
                        # holding a record from this second. The corrupt-line net was the thing that
                        # broke, so the lane it guards fails toward a false alarm on one bad byte,
                        # and jsonl_fraction fails the other way: the window lands past the bad line,
                        # total drops under min_records, and the lane stops judging in silence.
                        # The location goes in the exception so the single report still names it.
                        skips.add(ValueError(f"{os.path.basename(p)}:{i} unparseable: "
                                             f"{type(exc).__name__}: {exc}"))
        except Exception as exc:
            errlog.err(f"freshness: cannot read {p}", exc)


def check_newest_file(lane, skips):
    paths = expand(lane["glob"])
    if not paths:
        return f"no files match {lane['glob']}"
    newest = max(os.path.getmtime(p) for p in paths)
    age = (time.time() - newest) / HOUR
    if age > lane["max_age_h"]:
        return (f"newest file is {age:.1f} h old (limit {lane['max_age_h']} h) — "
                f"{os.path.basename(max(paths, key=os.path.getmtime))}")
    return None


ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def check_filename_period(lane, skips):
    """Age of the newest period an archive of dated files CLAIMS to cover, read from the filenames.

    For an archive whose members name the period they are evidence for, and whose arrival time is
    recorded nowhere else. `statements/amex/2026-05-11_2026-06-10.pdf` and
    `statements/halifax/2026-07-05.pdf` are both of that shape: the newest date in the basename is
    the end of the newest covered period, and nothing INSIDE the file is read — the lane asks
    whether the archive has grown, not whether a parse succeeded.

    `newest_file` is not a weaker version of this, it is a different measurement, and on a
    git-tracked archive it is a measurement of the checkout. Measured on the live tree 2026-10-05:
    mtime says the newest file under statements/amex is 1431.1 h old and names
    `2026-01-11_2026-02-10.pdf` — a FEBRUARY statement — because a checkout rewrites every mtime and
    does not preserve their order. statements/halifax reports the same 1431.1 h from a different
    file. Both numbers are the age of the checkout, so above any threshold the lane reads stale
    forever and below it reads ok forever, whatever the archive does. The periods themselves were
    2819.6 h (Amex) and 2219.6 h (Halifax) old at that moment, and neither card had gained a file in
    over three months. Same hazard check_json_stamp names, one step worse.

    A matching file whose basename carries no YYYY-MM-DD is counted into `skips` rather than
    ignored: it is a member of the archive this lane cannot see, and if it is the only one the lane
    breaches below on `newest is None`.

    THE TIP IS NOT THE ARCHIVE. Reducing the periods to max() answers "has the archive grown",
    which says nothing about its body: an archive with any number of missing middle periods reads
    ok the moment one recent file lands. That is not hypothetical here —
    `statements/halifax/2025-02-03.pdf -> 2025-04-03.pdf` is a 59-day step where the other 16
    consecutive steps in that archive are 28..33 d, and the mail corpus carries a Halifax
    "statement is ready" announcement on 2025-03-04 for the statement in between, which was never
    filed (moprox-memory/statement-lanes-max-only-blind-to-holes). The lane has never reported it,
    and this lane's own note in lanes.json already calls that hole "the class of thing this lane is
    for". So the interior steps are compared too, against the SAME `max_age_h`: no second knob to
    calibrate, and the threshold is conservative in the right direction, because an interior step
    has no availability lag in it. On a monthly archive 56 d cannot be reached without a period
    having been skipped, while the widest routine step observed on either card is 33 d.
    """
    paths = expand(lane["glob"])
    if not paths:
        return f"no files match {lane['glob']}"
    periods = []
    for p in paths:
        found = ISO_DATE.findall(os.path.basename(p))
        if not found:
            skips.add(ValueError(f"{os.path.basename(p)} carries no YYYY-MM-DD period in its name"))
            continue
        t = parse_ts(max(found))
        if t is not None:
            periods.append((t, os.path.basename(p)))
    if not periods:
        return (f"no usable YYYY-MM-DD period in the name of any of {len(paths)} file(s) "
                f"matching {lane['glob']}")
    periods.sort()
    limit = lane["max_age_h"]
    newest, newest_in = periods[-1]
    # Sorted by period, so consecutive pairs are consecutive covered periods whatever order the
    # glob came back in, and whatever the two naming conventions do to lexical order.
    holes = [f"{a_name} -> {b_name} ({(b - a) / HOUR / 24:.0f} d)"
             for (a, a_name), (b, b_name) in zip(periods, periods[1:]) if (b - a) / HOUR > limit]
    age = (time.time() - newest) / HOUR
    parts = []
    if age > limit:
        parts.append(f"newest covered period ends "
                     f"{datetime.fromtimestamp(newest, timezone.utc).date()}, {age:.1f} h ago "
                     f"(limit {limit} h) — {newest_in} of {len(paths)} file(s); that is what the "
                     f"archive SAYS it covers, not when the files were written")
    if holes:
        parts.append(f"{len(holes)} interior gap(s) wider than the same {limit} h limit, each one "
                     f"at least one period the archive never filed: {'; '.join(holes)}")
    return " | ".join(parts) or None


def check_jsonl_newest(lane, skips):
    """Age of the newest record — counting only records that carry data, if the lane says which.

    A date the *collector* synthesises (from a filename, a loop counter, `date.today()`) measures
    whether the collector ran, not whether anything arrived: handed a successful-but-empty response
    it still writes a row dated today, and the lane reads ok forever. `require` is how such a lane
    names the payload evidence that makes a row count. Lanes whose timestamp comes from the event
    itself — `notifications`, whose `ts` is the captured event's own clock — need no `require`,
    because there no event means no row.
    """
    paths = expand(lane["glob"])
    if not paths:
        return f"no files match {lane['glob']}"
    require = lane.get("require")
    newest, seen = None, 0
    for r in read_jsonl(paths, skips):
        seen += 1
        if require and not _match(r, require):
            continue
        t = parse_ts(r.get(lane["field"]))
        if t is not None and (newest is None or t > newest):
            newest = t
    if newest is None:
        if require and seen:
            return (f"{seen} record(s) present, none of them carrying data — no row satisfies "
                    f"`require` in {len(paths)} file(s)")
        return f"no usable '{lane['field']}' value in {len(paths)} file(s)"
    age = (time.time() - newest) / HOUR
    if age > lane["max_age_h"]:
        what = "newest record carrying data" if require else "newest record"
        return (f"{what} is {age:.1f} h old (limit {lane['max_age_h']} h), "
                f"at {datetime.fromtimestamp(newest, timezone.utc).isoformat(timespec='seconds')}")
    return None


OPERATORS = ("equals", "matches", "notnull", "at_least")


def _match(rec, spec):
    """Does one record satisfy one predicate? Raises on a predicate this checker cannot read.

    An unreadable spec used to fall off the end and return False for every record, which is not a
    non-match, it is "this lane is not being evaluated" wearing a non-match's clothes — and it is
    silent in the direction that matters. In a `where` clause every record is filtered out, `total`
    lands under `min_records`, check_jsonl_fraction returns None and the lane prints `ok`: one
    mistyped key switches a calibrated lane off for good with no journal record anywhere. The
    `require` and `predicate` positions fail the other way, into a confidently wrong breach —
    "N record(s) present, none of them carrying data" — whose named cause is the data.

    The main loop already applies exactly this discipline one level up: an unrecognised lane `kind`
    is an err-level line saying the lane is NOT being checked. Raising here routes an unrecognised
    operator into the same handler, so a config typo reaches the journal at err level and names the
    lane instead of being absorbed into an `ok`.
    """
    # `any_of` first: a composite spec names no field of its own.
    if "any_of" in spec:
        return any(_match(rec, s) for s in spec["any_of"])
    v = rec.get(spec["field"])
    if "equals" in spec:
        return v == spec["equals"]
    if "matches" in spec:
        return v is not None and re.search(spec["matches"], str(v)) is not None
    if "notnull" in spec:
        return (v is not None) == bool(spec["notnull"])
    if "at_least" in spec:
        try:
            return float(v) >= float(spec["at_least"])
        except (TypeError, ValueError):
            return False
    raise ValueError(f"predicate {sorted(spec)} names no operator this checker knows "
                     f"({'/'.join(OPERATORS)}/any_of) — the lane is NOT being evaluated")


def check_jsonl_fraction(lane, skips):
    """Of the records in the window that match `where`, what fraction satisfy `predicate`?

    A lane sets `min_fraction`, `max_fraction`, or both. `min_fraction` is a FLOOR and breaches when
    too FEW records satisfy the predicate — the degradation every other lane in this file watches
    for. `max_fraction` is a CEILING and breaches when too MANY do.

    The ceiling exists because lanes.json's own notifications note asked for it. The
    amex-alert-detail lane was a floor ("at least half of Amex alerts must carry an amount"); it
    did catch the 2026-08-06 changepoint, and then it had to be deleted on 2026-08-15 because the
    bare state is permanent and a floor held under a permanent breach fires forever. Nothing
    watched for the amounts coming BACK in the 56 days from then to 2026-10-10.

    That gap was an UNCONFIGURED LANE, not a missing operator, and the claim first committed here
    that the recovery was "inexpressible" with the old operator set is withdrawn. Breach direction
    is a property of the QUANTITY a lane measures, not of the operators: wherever the degraded
    state has a positive signature, a FLOOR on that signature already fires on recovery. It does
    here — post-changepoint, 320 of 326 Amex alerts are the one literal sentence "There was a
    transaction on your card ending with 11005." Measured 2026-10-10 against check.py at
    989309f6^, before `max_fraction` existed: the same where/window/min_records with
    predicate {matches: "There was a transaction"} and min_fraction 0.90 returns None on the live
    corpus and returns "only 10/85 (12%) ... (floor 90%)" when the 85 pre-changepoint alerts are
    replayed into the window. The ceiling's real value is the case that lane does NOT have — where
    "degraded" is the ABSENCE of a pattern and nothing positive names it, so there is no floor to
    put a threshold on — plus saying what it means in the direction a reader expects.

    Neither bound present is a config error, not an empty lane: it would compute a fraction and
    compare it against nothing, i.e. print `ok` forever. It raises, for the reason _match raises.
    """
    if "min_fraction" not in lane and "max_fraction" not in lane:
        raise ValueError("a jsonl_fraction lane must set min_fraction (floor), max_fraction "
                         "(ceiling) or both — the lane is NOT being evaluated")
    paths = expand(lane["glob"])
    if not paths:
        return f"no files match {lane['glob']}"
    cutoff = time.time() - lane["window_h"] * HOUR
    total = good = 0
    for r in read_jsonl(paths, skips):
        t = parse_ts(r.get(lane["field"]))
        if t is None or t < cutoff:
            continue
        if not _match(r, lane["where"]):
            continue
        total += 1
        good += bool(_match(r, lane["predicate"]))
    if total < lane.get("min_records", 1):
        return None                     # too little traffic to judge; not a breach
    frac = good / total
    floor, ceiling = lane.get("min_fraction"), lane.get("max_fraction")
    if floor is not None and frac < floor:
        return (f"only {good}/{total} ({frac:.0%}) of matching records in the last "
                f"{lane['window_h']} h satisfy the predicate (floor {floor:.0%})")
    if ceiling is not None and frac > ceiling:
        return (f"{good}/{total} ({frac:.0%}) of matching records in the last "
                f"{lane['window_h']} h satisfy the predicate, ABOVE the ceiling {ceiling:.0%}")
    return None


def check_json_newest(lane, skips):
    """Age of the newest record inside a JSON document on disk.

    For a DERIVED artifact — one JSON object holding a list of records, written by a script rather
    than appended to by a collector. `path` names the list inside the document ("" if the document
    is itself the list).

    `newest_file` cannot do this job. These artifacts are git-tracked, so their mtime is when the
    checkout last wrote them, which every pull moves and which says nothing about whether the
    producer ever ran again: a file regenerated once and then abandoned keeps a fresh mtime
    forever. Reading the records' own clock is the whole point.
    """
    paths = expand(lane["glob"])
    if not paths:
        return f"no files match {lane['glob']}"
    newest, newest_in, read = None, None, 0
    for p in paths:
        try:
            doc = json.loads(Path(p).read_text())
        except Exception as exc:
            # One unreadable artifact must not decide the lane for the others, and must not be
            # silent either. If it was the only one, `newest` stays None and the lane breaches below.
            errlog.err(f"freshness: cannot read {p}", exc)
            continue
        read += 1
        for key in [k for k in str(lane.get("path", "")).split(".") if k]:
            doc = doc.get(key) if isinstance(doc, dict) else None
        if not isinstance(doc, list):
            return (f"{os.path.basename(p)} holds no list at {lane.get('path', '')!r} — "
                    f"the artifact's shape is not what this lane was written against")
        for r in doc:
            t = parse_ts(r.get(lane["field"])) if isinstance(r, dict) else None
            if t is not None and (newest is None or t > newest):
                newest, newest_in = t, p
    if newest is None:
        return (f"no usable '{lane['field']}' value in {read} readable file(s) "
                f"of {len(paths)} matching {lane['glob']}")
    age = (time.time() - newest) / HOUR
    if age > lane["max_age_h"]:
        return (f"newest record in {os.path.basename(newest_in)} is {age:.1f} h old "
                f"(limit {lane['max_age_h']} h), at "
                f"{datetime.fromtimestamp(newest, timezone.utc).isoformat(timespec='seconds')} — "
                f"the file's own mtime says nothing here; nothing has regenerated it")
    return None


def check_json_stamp(lane, skips):
    """Age of a derived artifact's OWN generation stamp.

    The sibling kind above needs a list of dated records inside the document. A one-shot artifact
    need not have one: `finance/statements.json` is month×category×card aggregates with no record
    clock at all, and the only thing in it that knows when its producer ran is the scalar
    `generated: int(time.time())` that `finance/build.py` stamps at the top level. `field` is a
    dotted path to that scalar.

    `newest_file` is not a substitute here and the understatement is not small. Measured on
    finance/statements.json 2026-10-04: its own stamp says it was generated 714.0 h ago, its mtime
    says 568.3 h — 145.7 h of staleness that a mtime lane cannot see, because the checkout rewrote
    the file long after the producer last ran. Reading the producer's own clock is the whole point,
    for the same reason check_json_newest gives.
    """
    paths = expand(lane["glob"])
    if not paths:
        return f"no files match {lane['glob']}"
    newest, newest_in, read = None, None, 0
    for p in paths:
        try:
            doc = json.loads(Path(p).read_text())
        except Exception as exc:
            # One unreadable artifact must not decide the lane for the others, and must not be
            # silent either. If it was the only one, `newest` stays None and the lane breaches below.
            errlog.err(f"freshness: cannot read {p}", exc)
            continue
        read += 1
        for key in [k for k in str(lane["field"]).split(".") if k]:
            doc = doc.get(key) if isinstance(doc, dict) else None
        t = parse_ts(doc)
        if t is not None and (newest is None or t > newest):
            newest, newest_in = t, p
    if newest is None:
        return (f"no usable '{lane['field']}' stamp in {read} readable file(s) "
                f"of {len(paths)} matching {lane['glob']}")
    age = (time.time() - newest) / HOUR
    if age > lane["max_age_h"]:
        return (f"{os.path.basename(newest_in)} was generated {age:.1f} h ago "
                f"(limit {lane['max_age_h']} h), at "
                f"{datetime.fromtimestamp(newest, timezone.utc).isoformat(timespec='seconds')} — "
                f"that is the producer's own stamp; the file's mtime is younger and says nothing")
    return None


def check_http_json_newest(lane, skips):
    """Age of the newest record in a JSON array served by an estate HTTP service.

    For a lane whose store is a service rather than a file. The other kinds all read something on
    this box, and for such a lane there is nothing here whose mtime moves when the feed does — the
    consumer's own exit status is no evidence either, because a dead feed produces an empty work
    queue, which is exactly what a healthy idle run produces.

    An unreachable store is reported as a breach, not swallowed: from here it is indistinguishable
    from a store that is gone, and either way nothing is arriving. Which one it was is in `detail`.
    """
    url = lane["url"]
    try:
        recs = json.loads(DIRECT.open(url, timeout=lane.get("timeout_s", 60)).read())
    except Exception as exc:
        return f"cannot read {url} — {type(exc).__name__}: {exc}"
    if not isinstance(recs, list):
        return f"{url} did not answer with a JSON array — got {type(recs).__name__}"
    if not recs:
        return f"{url} returned no records"
    newest = None
    for r in recs:
        t = parse_ts(r.get(lane["field"]))
        if t is not None and (newest is None or t > newest):
            newest = t
    if newest is None:
        return f"no usable '{lane['field']}' value in {len(recs)} record(s) from {url}"
    age = (time.time() - newest) / HOUR
    if age > lane["max_age_h"]:
        return (f"newest record is {age:.1f} h old (limit {lane['max_age_h']} h), "
                f"at {datetime.fromtimestamp(newest, timezone.utc).isoformat(timespec='seconds')} "
                f"— {len(recs)} record(s) at {url}")
    return None


KINDS = {"newest_file": check_newest_file,
         "filename_period": check_filename_period,
         "jsonl_newest": check_jsonl_newest,
         "jsonl_fraction": check_jsonl_fraction,
         "json_newest": check_json_newest,
         "json_stamp": check_json_stamp,
         "http_json_newest": check_http_json_newest}


JOURNAL_SOCKET = "/run/systemd/journal/socket"


def journal_field(key, value):
    """One field in journald's native wire format.

    `KEY=value\\n` cannot carry a newline, and a breach detail is free text built from whatever the
    lane check found, so a value containing one is sent in the binary form journald also accepts:
    the key, a newline, a 64-bit little-endian length, the raw bytes, a newline.
    """
    raw = str(value).encode("utf-8", "replace")
    if b"\n" in raw:
        return key.encode() + b"\n" + len(raw).to_bytes(8, "little") + raw + b"\n"
    return key.encode() + b"=" + raw + b"\n"


def journal_send(fields):
    """Hand one record to journald FROM THIS PROCESS, not from a child.

    journald does not trust what a sender says about itself: it reads the sender's pid out of the
    socket's SCM_CREDENTIALS and looks that pid's cgroup up to fill in `_SYSTEMD_UNIT` and
    `_SYSTEMD_INVOCATION_ID`. It does that when it DEQUEUES the datagram, which is not when the
    datagram was sent. `logger --journald` — what this used to shell out to — has already exited by
    then, so under any queueing at all the lookup finds no such pid and the record lands with no
    unit and no invocation id on it.

    That is not theoretical. On claude-dev, of 74 `lane polar STALE` records over 2026-09-04..08,
    14 arrived with no trusted unit, interleaved run by run — and an incident whose newest record
    lost its invocation id is one `get_incident_detail()` answers with `rows: []`, which is the same
    answer as a lane that never fired (moprox-memory/lane-stale-detail-unreachable.md). Sending
    from this long-lived process gives journald a pid that is still there to resolve.
    """
    payload = b"".join(journal_field(k, v) for k, v in fields)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
    try:
        sock.sendto(payload, JOURNAL_SOCKET)
    finally:
        sock.close()


def raise_incident(lane, detail):
    """Log a journal record the sink aggregates as a lane-stale incident."""
    msg = f"lane {lane['name']} STALE: {detail}"
    if lane.get("note"):
        msg += f" | {lane['note']}"
    fields = [("MESSAGE_ID", MSGID_LANE_STALE), ("PRIORITY", "3"),
              ("UNIT", f"lane-{lane['name']}"), ("LANE", lane["name"]),
              ("MESSAGE", msg)]
    try:
        journal_send(fields)
    except Exception as exc:
        # Delivering the breach matters more than its attribution, so keep the old child as the
        # fallback — but say at err that we took it. A record too big for a datagram, or a missing
        # journal socket, is not a thing this should absorb quietly.
        errlog.err(f"freshness: in-process journal send failed for lane {lane['name']}, falling "
                   f"back to logger(1) — the record may land with no unit attached", exc)
        try:
            blob = "".join(f"{k}={v}\n" for k, v in fields)
            subprocess.run(["logger", "--journald"], input=blob, text=True, check=True, timeout=20)
        except Exception as exc2:
            # The whole point is that a degraded lane becomes visible; if we cannot say so, say THAT.
            errlog.err(f"freshness: could not raise the incident for lane {lane['name']} ({detail})",
                       exc2)


def main():
    dry = "--dry" in sys.argv[1:]
    skips = errlog.Skips("freshness: reading lane files")
    try:
        cfg = json.loads(LANES.read_text())
    except Exception as exc:
        errlog.err(f"freshness: cannot read {LANES} — NO lane is being checked", exc)
        return 1

    breaches = 0
    for lane in cfg.get("lanes", []):
        fn = KINDS.get(lane.get("kind"))
        if fn is None:
            errlog.err(f"freshness: lane {lane.get('name')} has unknown kind "
                       f"{lane.get('kind')!r} — it is NOT being checked",
                       ValueError(lane.get("kind")))
            continue
        try:
            detail = fn(lane, skips)
        except Exception as exc:
            errlog.err(f"freshness: check for lane {lane.get('name')} raised", exc)
            continue
        if detail:
            breaches += 1
            print(f"STALE  {lane['name']}: {detail}")
            if not dry:
                raise_incident(lane, detail)
        else:
            print(f"ok     {lane['name']}")

    # Say the skipped lines out loud, once. This object was constructed above and then never
    # reported, so even with the call site above fixed a corrupt lane file would have been counted
    # into silence — the second half of the same swallow. No `total` here on purpose: it would be
    # a count across every lane's files and "ALL n records unusable" is not a claim this loop is in
    # a position to make. A lane with nothing left readable already breaches on its own, at err.
    skips.report()
    print(f"{breaches} breach(es) across {len(cfg.get('lanes', []))} lane(s)")
    return 0            # breaches are reported as incidents, not as a unit failure


if __name__ == "__main__":
    sys.exit(main())
