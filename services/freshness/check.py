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
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog

LANES = Path(__file__).resolve().parent / "lanes.json"
MSGID_LANE_STALE = "6d0a9d7d5f1c4a3b8e2c74f0a1b93e55"     # must match logview/server.py
HOUR = 3600.0


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
                        skips.skip(f"{os.path.basename(p)}:{i} unparseable", exc)
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


def check_jsonl_newest(lane, skips):
    paths = expand(lane["glob"])
    if not paths:
        return f"no files match {lane['glob']}"
    newest = None
    for r in read_jsonl(paths, skips):
        t = parse_ts(r.get(lane["field"]))
        if t is not None and (newest is None or t > newest):
            newest = t
    if newest is None:
        return f"no usable '{lane['field']}' value in {len(paths)} file(s)"
    age = (time.time() - newest) / HOUR
    if age > lane["max_age_h"]:
        return (f"newest record is {age:.1f} h old (limit {lane['max_age_h']} h), "
                f"at {datetime.fromtimestamp(newest, timezone.utc).isoformat(timespec='seconds')}")
    return None


def _match(rec, spec):
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
    return False


def check_jsonl_fraction(lane, skips):
    """Of the records in the window that match `where`, what fraction satisfy `predicate`?"""
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
    if frac < lane["min_fraction"]:
        return (f"only {good}/{total} ({frac:.0%}) of matching records in the last "
                f"{lane['window_h']} h satisfy the predicate (floor {lane['min_fraction']:.0%})")
    return None


KINDS = {"newest_file": check_newest_file,
         "jsonl_newest": check_jsonl_newest,
         "jsonl_fraction": check_jsonl_fraction}


def raise_incident(lane, detail):
    """Log a journal record the sink aggregates as a lane-stale incident."""
    msg = f"lane {lane['name']} STALE: {detail}"
    if lane.get("note"):
        msg += f" | {lane['note']}"
    fields = "\n".join([f"MESSAGE_ID={MSGID_LANE_STALE}", "PRIORITY=3",
                        f"UNIT=lane-{lane['name']}", f"LANE={lane['name']}",
                        f"MESSAGE={msg}"]) + "\n"
    try:
        subprocess.run(["logger", "--journald"], input=fields, text=True, check=True, timeout=20)
    except Exception as exc:
        # The whole point is that a degraded lane becomes visible; if we cannot say so, say THAT.
        errlog.err(f"freshness: could not raise the incident for lane {lane['name']} ({detail})", exc)


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

    print(f"{breaches} breach(es) across {len(cfg.get('lanes', []))} lane(s)")
    return 0            # breaches are reported as incidents, not as a unit failure


if __name__ == "__main__":
    sys.exit(main())
