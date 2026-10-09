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

case("retire_and_say is idempotent — the second call closes nothing more",
     loop.retire_and_say(led, 597, "test")[0], [])

# --- answered_elsewhere: the one-hop blind spot. c610 was answered at c612, c612's own objection
# was answered at c615, c615 was accepted — and the rule still cannot close c610, so the digest
# must at least say so rather than offer it as untouched work.
led3 = {"accepted": [{"cycle": 615, "claim": "The cycle-612 [claim] objection is UPHELD."}],
        "disputed": [{"cycle": 610, "claim": "a"}, {"cycle": 609, "claim": "b"},
                     {"cycle": 611, "claim": "untouched"}],
        "resolved": [{"cycle": 612, "claim": "The cycle-610 [claim] objection is UPHELD.",
                      "answered_by": 615}]}
pend = loop.answered_elsewhere(led3)
case("a two-link chain is annotated, not closed", set(pend), {610})
case("the chain names the accepted terminus",
     ["c615 — ACCEPTED" in pend[610][0]], [True])
case("retire_answered still leaves it open",
     set(loop.retire_answered(led3, 616)[0]), set())
dig = __import__("json").loads(loop.ledger_digest(led3))
case("the digest carries it on the disputed entry",
     [e.get("already_answered_by") for e in dig["disputed"] if e["cycle"] == 610],
     [pend[610]])
case("and says nothing about a dispute nobody answered",
     [e.get("already_answered_by") for e in dig["disputed"] if e["cycle"] == 611], [None])
# an answer that is itself still disputed is reported as such, not as settled
led3["resolved"] = []
led3["disputed"].append({"cycle": 612, "claim": "The cycle-610 [claim] objection is UPHELD."})
case("an unaccepted, unanswered answer is flagged as still disputed",
     [loop.answered_elsewhere(led3)[610]], [["c612 (itself still disputed)"]])

# --- the call ORDER in main(): a retirement must reach the prompt of the cycle that earns it.
# retire_answered used to be called once, after the agent had already been handed the digest, so
# every closure was one cycle late and a change to the rule was two (the cycle that lands it runs
# the pre-fix interpreter). Source order is the cheapest pin that would actually catch a regression.
src = (Path(__file__).resolve().parents[1] / "loop.py").read_text()
body = src[src.index("\ndef main():"):]
case("retire runs before the digest is built",
     [body.index("retire_and_say(led, cyc, agent)") < body.index("ledger_digest(led)")], [True])
case("and again after the cycle's own proposals are judged",
     [body.count("retire_and_say(led, cyc, agent)")], [2])

print("\n%d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
