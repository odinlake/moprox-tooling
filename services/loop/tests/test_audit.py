#!/usr/bin/env python3
"""The audit gate's fail-closed property, exercised over every way a refuter can die.

    python3 services/loop/tests/test_audit.py

No agent is spawned: `subprocess.run` is replaced so each of refute()'s failure paths can be driven
deterministically, and log_usage is stubbed so nothing reaches the shared usage ledger.

What this pins is the fix for analyst cycle 378 (moprox-memory/audit-lens-failure-scores-as-approval).
refute() had exactly one non-None return — the objection — and returned None on FOUR distinct
failure paths as well: timeout, non-zero exit, no JSON verdict, unparseable verdict. adversarial()
tested only `if d:`, so a skeptic that died was scored identically to a skeptic that looked and let
the claim stand. The gate failed OPEN, and hardest on the hardest claims, since the refuter doing
the most reading is the one that hits REFUTE_MAX_S. Measured 2026-08-08..09-06: 8 of 248 audited
cycles logged only one of their two refuter runs; 6 were published as accepted estate facts.

So the property under test is not "a dead lens is reported" — it was always reported, by warn().
It is that a dead lens **blocks**: adversarial() must return a non-empty objection list, which is
what makes the caller record the claim as `disputed` instead of publishing it.
"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

os.environ["LOOP_ADVERSARIAL"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import loop

tmp = Path(tempfile.mkdtemp(prefix="audit-"))
loop.OBJECTIONS = tmp / "objections"
loop.log_usage = lambda *a, **k: None
loop.LENSES = [("check", "does the verifier test the claim"), ("claim", "does the claim overrun")]

passed = failed = 0
def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS  {label}" + (f" [{detail}]" if detail else ""))
    else:
        failed += 1
        print(f"FAIL  {label}" + (f" [{detail}]" if detail else ""))

class Done:
    def __init__(self, rc, stdout, stderr=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr

def envelope(verdict_text):
    return json.dumps({"result": verdict_text, "usage": {}, "total_cost_usd": 0.0})

def verdict(refuted, defect=""):
    return envelope("I read the thing.\n" + json.dumps({"refuted": refuted, "defect": defect}))

def with_run(fn):
    """Run adversarial() with subprocess.run replaced by fn. -> the objection list."""
    real = subprocess.run
    subprocess.run = fn
    try:
        return loop.adversarial({"claim": "c", "why": "w", "verify": "v", "expect": "e"},
                                "evidence", "analyst", cyc=0)
    finally:
        subprocess.run = real

PROP_DEAD = [
    ("timeout",
     lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd="claude", timeout=420)),
     "timed out"),
    ("non-zero exit",
     lambda *a, **k: Done(1, "", "Invalid API key · Please run /login"),
     "exited 1"),
    ("no JSON verdict",
     lambda *a, **k: Done(0, envelope("I could not decide, sorry.")),
     "no JSON verdict"),
    ("unparseable verdict",
     lambda *a, **k: Done(0, envelope("here you go: {refuted: yes,}")),
     "not valid JSON"),
]

print("--- every failure path blocks -------------------------------------------")
for label, fn, symptom in PROP_DEAD:
    objs = with_run(fn)
    check(f"{label}: blocks", len(objs) == 2, f"{len(objs)} objection(s)")
    # `objs and` on purpose: all() over [] is True, so without it these two would pass vacuously
    # against the very code they exist to catch — the pre-fix gate returns exactly [].
    check(f"{label}: says the lens did not complete",
          bool(objs) and all("LENS DID NOT COMPLETE" in o for o in objs))
    check(f"{label}: names the symptom {symptom!r}",
          bool(objs) and all(symptom in o for o in objs),
          (objs[0][:110] if objs else "none"))

print("\n--- a lens that dies is NOT scored as a lens that approved ---------------")
# The exact confusion of cycle 378: one lens dies, the other looks and approves. Before the fix
# both produced None and the claim published as "audited by two independent skeptics".
calls = {"n": 0}
def one_dies(*a, **k):
    calls["n"] += 1
    if calls["n"] == 1:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=420)
    return Done(0, verdict(False))
objs = with_run(one_dies)
check("one dead + one approving is not a clean audit", len(objs) == 1, f"{len(objs)} objection(s)")
check("and the surviving lens is not the one blocking",
      bool(objs) and objs[0].startswith("[check]"), objs[0][:60] if objs else "none")

print("\n--- the two verdict paths still behave ----------------------------------")
objs = with_run(lambda *a, **k: Done(0, verdict(False)))
check("both lenses approve -> published", objs == [], f"{len(objs)} objection(s)")

objs = with_run(lambda *a, **k: Done(0, verdict(True, "the expect string cannot fail")))
check("both lenses refute -> disputed", len(objs) == 2, f"{len(objs)} objection(s)")
check("and a real defect is not dressed as a dead lens",
      all("LENS DID NOT COMPLETE" not in o for o in objs))
check("objections are archived verbatim",
      sorted(p.name for p in (tmp / "objections").glob("c0-*.txt")) != [],
      ", ".join(sorted(p.name for p in (tmp / "objections").glob("c0-*.txt"))))

print("\n--- refute() itself distinguishes the three outcomes ---------------------")
real = subprocess.run
try:
    subprocess.run = lambda *a, **k: Done(0, verdict(False))
    check("survived -> None",
          loop.refute({"claim": "c"}, "e", ("check", "f"), "analyst") is None)
    subprocess.run = lambda *a, **k: Done(0, verdict(True, "hard-coded value"))
    check("refuted -> string",
          loop.refute({"claim": "c"}, "e", ("check", "f"), "analyst") == "[check] hard-coded value")
    subprocess.run = lambda *a, **k: Done(2, "", "boom")
    try:
        loop.refute({"claim": "c"}, "e", ("check", "f"), "analyst")
        check("died -> LensFailed", False, "returned instead of raising")
    except loop.LensFailed as exc:
        check("died -> LensFailed", True, str(exc)[:60])
finally:
    subprocess.run = real

print(f"\n{passed}/{passed + failed} passed")
sys.exit(1 if failed else 0)
