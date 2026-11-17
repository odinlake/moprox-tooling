#!/usr/bin/env python3
"""check_jsonl_newest's `min_distinct_hours` floor, on synthetic jsonl.

    python3 services/freshness/test_distinct_hours.py

A wake-burst from a dying device is a few genuine rows inside seconds; on max(ts) alone it buys the
whole max_age_h of green (i-20261117-150857). The floor makes the lane want rows in several distinct
clock hours. Without the key the lane must behave exactly as before.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check

H = 3600.0
results = []


def case(name, got, want):
    ok = got == want
    results.append(ok)
    print(("ok   " if ok else "FAIL ") + f"{name}: got {got!r} want {want!r}")


def lane(tmp, **kw):
    base = {"name": "t", "kind": "jsonl_newest", "glob": str(tmp), "field": "ts",
            "max_age_h": 24, "min_distinct_hours": 3}
    base.update(kw)
    return base


def write(tmp, stamps):
    with open(tmp, "w") as fh:
        for t in stamps:
            fh.write(json.dumps({"ts": t, "app": "x"}) + "\n")


def run(l):
    return check.check_jsonl_newest(l, check.errlog.Skips("test"))


def hour_start(k):
    """Start of the clock hour k hours before the current one, plus 60 s."""
    return (int(time.time() // H) - k) * H + 60


with tempfile.TemporaryDirectory() as d:
    tmp = Path(d) / "notif.jsonl"
    now = time.time()

    # --- the 11-14 burst: 2 rows 4 s apart, 2 h ago. Old rule: ok. Floor: breach.
    write(tmp, [now - 2 * H, now - 2 * H + 4])
    got = run(lane(tmp)) or ""
    case("burst of 2 rows breaches", got.startswith("rows in only 1 distinct clock hour(s)"), True)
    case("breach names the floor", "(floor 3)" in got, True)
    case("burst with no earlier rows: never met", got.endswith("never met in the archive"), True)

    # --- 6 rows in 2 distinct hours (the two 11-16 bursts): still a breach. K=2 would clear it.
    write(tmp, [hour_start(2) + i for i in range(2)] + [hour_start(1) + i for i in range(4)])
    got = run(lane(tmp)) or ""
    case("6 rows in 2 hours breaches", got.startswith("rows in only 2 distinct"), True)

    # --- rows in 3 distinct hours: ok.
    write(tmp, [hour_start(5), hour_start(3), hour_start(1)])
    case("3 distinct hours is ok", run(lane(tmp)), None)

    # --- last-met time: a steady 36-min tick that stops 100 h ago, then a burst 1 h ago.
    stop = now - 100 * H
    write(tmp, [stop - i * 36 * 60 for i in range(60)] + [now - H, now - H + 4])
    got = run(lane(tmp)) or ""
    want = f"last met at {check._iso(stop)}"
    case("names when the floor was last met", want in got, True)

    # --- same lane WITHOUT the key, a row 1 h ago: unchanged, ok.
    write(tmp, [now - H])
    case("no key, one recent row is ok", run(lane(tmp, min_distinct_hours=None)), None)
    l = lane(tmp); del l["min_distinct_hours"]
    case("key absent, one recent row is ok", run(l), None)

    # --- newest older than max_age_h: the old message, floor or not.
    write(tmp, [now - 30 * H])
    got = run(lane(tmp)) or ""
    case("old row gives the old message", got.startswith("newest record is 30.0 h old (limit 24 h)"),
         True)
    l = lane(tmp); del l["min_distinct_hours"]
    case("old message byte-identical with and without the key", run(l), got)

    # --- `require` still filters what counts toward the floor.
    with open(tmp, "w") as fh:
        for k in (5, 3, 1):
            fh.write(json.dumps({"ts": hour_start(k), "app": "other"}) + "\n")
        fh.write(json.dumps({"ts": now - 60, "app": "x"}) + "\n")
    got = run(lane(tmp, require={"field": "app", "equals": "x"})) or ""
    case("require applies to the floor", got.startswith("rows in only 1 distinct"), True)

    # --- a malformed floor is a config error, not an ok.
    for bad in ("3", 0, True):
        try:
            run(lane(tmp, min_distinct_hours=bad))
            case(f"bad floor {bad!r} raises", "returned", "ValueError")
        except ValueError:
            case(f"bad floor {bad!r} raises", True, True)

print(f"{sum(results)}/{len(results)} ok")
sys.exit(0 if all(results) else 1)
