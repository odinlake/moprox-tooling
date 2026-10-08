#!/usr/bin/env python3
"""answered_cycles' two idioms and retire_answered's bookkeeping, on synthetic ledgers.

    python3 services/loop/tests/test_retire.py

The rule has been wrong in three different directions (bare numbers closing citations, adjacency
reading prose about disputes as an answer, and recognising only rebuttals), and each repair was
measured on the live ledger and then lost. These are the cases that must keep holding.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import loop

results = []


def case(name, got, want):
    ok = got == want
    results.append(ok)
    print(("ok   " if ok else "FAIL ") + f"{name}: got {sorted(got)} want {sorted(want)}")


A = loop.answered_cycles

# --- the rebuttal idiom: a cycle beside the word "objection", in the opening clause
case("single rebuttal", A("The cycle-580 [check] objection is UPHELD on both prongs."), {580})
case("plural rebuttal", A("Both cycle-540 objections are UPHELD and reproduce."), {540})
case("chained rebuttal — every cycle named, not just the last",
     A("The cycle-592 and cycle-593 objections are UPHELD and both arms fall."), {592, 593})

# --- the supersession idiom: an UPPERCASE disposition verb, anywhere in the claim
case("supersession mid-claim", A("x" * 400 + "LEG2 of c592/c593 is therefore UNIDENTIFIED."),
     {592, 593})
case("withdrawal", A("c593's LEG2 is WITHDRAWN as an anchoring artefact."), {593})
case("supersession", A("c596 is SUPERSEDED and must not be re-proposed. c595 says why."), {596})

# --- what must NOT close a dispute
case("bare citation", A("since c538 established wattbike.py writes no log, nothing changed"), set())
case("prose about a dispute, outside the opening",
     A("x" * 300 + "would have closed c584, whose objection nothing has answered"), set())
case("lower-case disposition", A("c592's LEG2 was withdrawn quietly, in prose"), set())
case("verb and subject in different sentences",
     A("c580's LEG3 is WITHDRAWN. But that gap is untestable by c359."), {580})

# --- retire_answered: moves, records the answerer, and is reversible
led = {"accepted": [{"cycle": 595, "claim": "LEG2 of c592/c593 is UNIDENTIFIED."},
                    {"cycle": 596, "claim": "The cycle-593 objection is UPHELD."}],
       "disputed": [{"cycle": 592, "claim": "a"}, {"cycle": 593, "claim": "b"},
                    {"cycle": 594, "claim": "c"}],
       "resolved": []}
closed, reopened = loop.retire_answered(led, 597)
case("retire closes the superseded pair", set(closed), {592, 593})
case("retire reopens nothing", set(reopened), set())
case("the unanswered dispute stays open", {e["cycle"] for e in led["disputed"]}, {594})
case("the answerer is recorded", {e["answered_by"] for e in led["resolved"]}, {595})

# an answerer that stops qualifying reopens its entry; one that aged off the ring does not
led2 = {"accepted": [{"cycle": 595, "claim": "nothing is disposed of here"}],
        "disputed": [], "resolved": [{"cycle": 592, "claim": "a", "answered_by": 595},
                                     {"cycle": 500, "claim": "b", "answered_by": 501}]}
closed, reopened = loop.retire_answered(led2, 598)
case("a disconfirmed closure reopens", set(reopened), {592})
case("an unprovable closure stays closed", {e["cycle"] for e in led2["resolved"]}, {500})

print("\n%d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
