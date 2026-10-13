#!/usr/bin/env python3
"""check_json_dir_newest, on a synthetic directory of one-record-per-file JSON.

    python3 services/freshness/test_json_dir.py

The lane this exists for is `technogym-cardio`, which was `newest_file` until 2026-10-13 and so
measured the wrong two things: the file's mtime instead of the record's own clock, and the
file's existence instead of whether it carried a measurement. Technogym writes
`{"sport": "", "activities": []}` for a session whose cardio detail came back empty, and the
fetcher never revisits an idCr it has written, so each hollow record silently reset that lane's
clock for a week. The cases that matter are therefore:

  - a recent HOLLOW record must NOT clear the lane (the regression that was live for 20 days);
  - a stale mtime on a fresh record must not breach, and a fresh mtime on a stale record must
    breach — i.e. mtime is not consulted at all;
  - a directory of hollow records only must say so, not "no usable field value";
  - the vendor's own `startedOn` format, space before the offset, must parse.
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check

results = []


def case(name, got, want):
    ok = got == want
    results.append(ok)
    print(("ok   " if ok else "FAIL ") + f"{name}: got {got!r} want {want!r}")


def lane(d, **kw):
    base = {"name": "t", "kind": "json_dir_newest", "glob": str(Path(d) / "*.json"),
            "field": "startedOn", "require": {"field": "activities", "nonempty": True},
            "max_age_h": 168}
    base.update(kw)
    return base


def session(d, idcr, hours_ago, hollow, mtime_hours_ago=None):
    """One record `hours_ago` hours old, in technogym's exact on-disk shape."""
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).astimezone()
    stamp, offset = ts.isoformat(timespec="seconds")[:19], ts.isoformat(timespec="seconds")[19:]
    rec = {"idCr": idcr,
           # `2026-10-06 12:11:45 +01:00` — space separator AND a space before the offset, as the
           # vendor writes it. Not ISO 8601, and the reason parse_ts normalises.
           "startedOn": stamp.replace("T", " ") + " " + offset,
           "facilityId": "ce141d34-825c-459f-9b4f-80e093b59a00",
           "sport": "" if hollow else "run",
           "activities": [] if hollow else [{"machine": "T1", "equipment": "Treadmill",
                                             "durationS": 2800, "distanceM": 8000}]}
    p = Path(d) / f"{idcr}.json"
    p.write_text(json.dumps(rec))
    if mtime_hours_ago is not None:
        t = time.time() - mtime_hours_ago * 3600
        os.utime(p, (t, t))
    return p


with tempfile.TemporaryDirectory() as d:
    # --- the live 2026-09-28..10-13 state: last real session 482 h back, three hollow records
    # since, the newest of them 169 h back. `newest_file` called this 169.4 h and ok for 13 of
    # those days; the content clock calls it what it is.
    session(d, 1121, 482.7, hollow=False)
    session(d, 1122, 361.0, hollow=True)
    session(d, 1123, 310.0, hollow=True)
    session(d, 1124, 169.4, hollow=True)
    got = check.check_json_dir_newest(lane(d), None)
    case("hollow records do not clear the lane", bool(got) and "482.7 h" in got, True)
    case("breach names the hollow count", "3 of 4 file(s) carry no data" in (got or ""), True)
    case("breach names the record, not the newest file", "1121.json" in (got or ""), True)

    # --- a fresh session carrying data clears it, and the three hollow records do not interfere.
    session(d, 1125, 2.0, hollow=False)
    case("a real session clears it", check.check_json_dir_newest(lane(d), None), None)

with tempfile.TemporaryDirectory() as d:
    # --- mtime is not consulted in either direction. Both of these would be wrong under
    # `newest_file`: a fresh clone (mtime = now, data ancient) reads ok forever, and the box that
    # merely rewrote the file reads stale on data that is current.
    session(d, 1, 900.0, hollow=False, mtime_hours_ago=0.0)
    case("ancient data, fresh mtime: breaches", bool(check.check_json_dir_newest(lane(d), None)), True)

with tempfile.TemporaryDirectory() as d:
    session(d, 1, 1.0, hollow=False, mtime_hours_ago=5000.0)
    case("current data, ancient mtime: ok", check.check_json_dir_newest(lane(d), None), None)

with tempfile.TemporaryDirectory() as d:
    # --- hollow-ONLY is its own sentence. Falling through to "no usable 'startedOn' value" would
    # blame the field, i.e. name the archive's shape for what is a collection failure.
    session(d, 1, 2.0, hollow=True)
    session(d, 2, 3.0, hollow=True)
    got = check.check_json_dir_newest(lane(d), None)
    case("hollow-only says none carry data", "none of them carrying data" in (got or ""), True)
    # --- and without `require` the same directory is ok: the predicate is what makes it a breach,
    # so a lane that forgets it degrades to the old behaviour loudly rather than silently.
    cfg = lane(d); cfg.pop("require")
    case("no require: hollow records count", check.check_json_dir_newest(cfg, None), None)

with tempfile.TemporaryDirectory() as d:
    # --- an empty glob is its own breach, and a member of the wrong shape names the shape rather
    # than being counted as a non-match.
    case("empty glob", "no files match" in check.check_json_dir_newest(lane(d), None), True)
    (Path(d) / "list.json").write_text("[1, 2, 3]")
    got = check.check_json_dir_newest(lane(d), None)
    case("a list member names the shape", "not one JSON object" in (got or ""), True)

# --- the operator vocabulary: `nonempty` is what `notnull` could not say.
case("nonempty rejects []", check._match({"a": []}, {"field": "a", "nonempty": True}), False)
case("nonempty accepts [x]", check._match({"a": [1]}, {"field": "a", "nonempty": True}), True)
case("notnull cannot see it", check._match({"a": []}, {"field": "a", "notnull": True}), True)
case("vendor startedOn parses", check.parse_ts("2026-10-06 12:11:45 +01:00"),
     datetime.fromisoformat("2026-10-06T12:11:45+01:00").timestamp())

print(f"{sum(results)}/{len(results)} ok")
sys.exit(0 if all(results) else 1)
