#!/usr/bin/env python3
"""check_jsonl_fraction's two directions, on synthetic jsonl.

    python3 services/freshness/test_fraction.py

The floor (`min_fraction`) is the degradation case and was all this checker had. The ceiling
(`max_fraction`) fires on a condition IMPROVING, which is what the amex-alert-detail-returned lane
needs and what lanes.json's notifications note recorded as impossible until 2026-10-10. Both of the
cases that matter are here: a ceiling must actually fire when the fraction rises past it, and a lane
that sets NEITHER bound must raise rather than evaluate to `ok` forever.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check

results = []


def case(name, got, want):
    ok = got == want
    results.append(ok)
    print(("ok   " if ok else "FAIL ") + f"{name}: got {got!r} want {want!r}")


def lane(tmp, **kw):
    """A jsonl_fraction lane over `tmp`, window wide enough to hold everything written."""
    base = {"name": "t", "kind": "jsonl_fraction", "glob": str(tmp), "field": "ts",
            "window_h": 24, "where": {"field": "app", "equals": "amex"},
            "predicate": {"field": "text", "matches": r"[£$€]\s?[0-9]"}, "min_records": 4}
    base.update(kw)
    return base


def write(tmp, detailed, bare, other=0):
    """`detailed` alerts carrying an amount, `bare` without, `other` from another app."""
    now = time.time()
    with open(tmp, "w") as fh:
        for i in range(detailed):
            fh.write(json.dumps({"ts": now - i, "app": "amex",
                                 "text": f"You have a £{i}.41 charge at SAINSBURYS."}) + "\n")
        for i in range(bare):
            fh.write(json.dumps({"ts": now - i, "app": "amex",
                                 "text": "There was a transaction on your card."}) + "\n")
        for i in range(other):
            fh.write(json.dumps({"ts": now - i, "app": "telegram", "text": "£5 for lunch?"}) + "\n")


def run(l):
    return check.check_jsonl_fraction(l, check.errlog.Skips("test"))


with tempfile.TemporaryDirectory() as d:
    tmp = Path(d) / "notif.jsonl"

    # --- the ceiling can fire. The whole point: it is silent on the permanent bare state and loud
    # the moment detail returns, which no operator in this checker could express before.
    write(tmp, detailed=0, bare=69)
    case("ceiling silent while every alert is bare", run(lane(tmp, max_fraction=0.10)), None)
    write(tmp, detailed=3, bare=66)                 # the monthly billing notices, 4.3%
    case("ceiling clears the billing cycle", run(lane(tmp, max_fraction=0.10)), None)
    write(tmp, detailed=12, bare=57)                # detail is back
    got = run(lane(tmp, max_fraction=0.10))
    case("ceiling FIRES when detail returns", (got or "").split(" of")[0], "12/69 (17%)")
    case("ceiling breach says ABOVE, not 'only'", "ABOVE the ceiling 10%" in (got or ""), True)

    # --- the floor is unchanged, including that it is the opposite direction on the same data.
    write(tmp, detailed=0, bare=69)
    got = run(lane(tmp, min_fraction=0.50))
    case("floor fires on the bare state", (got or "").startswith("only 0/69 (0%)"), True)
    write(tmp, detailed=60, bare=9)
    case("floor silent when detail is present", run(lane(tmp, min_fraction=0.50)), None)

    # --- both bounds at once: a band.
    write(tmp, detailed=35, bare=34)
    case("inside the band", run(lane(tmp, min_fraction=0.20, max_fraction=0.80)), None)

    # --- a lane naming neither bound computes a fraction and compares it to nothing. It must raise:
    # the main loop turns that into an err-level line naming the lane, where returning None would
    # print `ok` for a lane nobody is checking.
    write(tmp, detailed=12, bare=57)
    try:
        run(lane(tmp))
        case("no bound raises", "returned", "ValueError")
    except ValueError as exc:
        case("no bound raises", "NOT being evaluated" in str(exc), True)

    # --- and it raises BEFORE the glob is consulted, so a config typo is loud even on a lane whose
    # files have moved: an empty glob returns its own breach string and would mask the typo.
    try:
        run(lane(Path(d) / "nope-*.jsonl"))
        case("no bound raises ahead of the glob check", "returned", "ValueError")
    except ValueError:
        case("no bound raises ahead of the glob check", True, True)

    # --- min_records still gates: too little traffic is not a verdict in either direction.
    write(tmp, detailed=2, bare=0)
    case("below min_records, ceiling abstains", run(lane(tmp, max_fraction=0.10)), None)

print(f"{sum(results)}/{len(results)} ok")
sys.exit(0 if all(results) else 1)
