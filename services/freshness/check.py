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


def _match(rec, spec):
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
         "jsonl_newest": check_jsonl_newest,
         "jsonl_fraction": check_jsonl_fraction,
         "http_json_newest": check_http_json_newest}


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
