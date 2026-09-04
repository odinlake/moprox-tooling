#!/usr/bin/env python3
"""bind_state's one-time migration and the cross-agent mutex, against a fake ROOT.

    python3 services/loop/tests/test_state.py

The migration moves the analyst's live ledger, so the case that matters is that it is idempotent
and loses nothing; the mutex matters across PROCESSES, which is the last case here.
"""
import os, sys, json, tempfile, shutil, subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import loop

tmp = Path(tempfile.mkdtemp(prefix="state-"))
loop.ROOT = tmp
loop.LOCK = tmp / "loop.lock"
ok = []

# legacy layout: everything directly under ROOT, as it was until 2026-09-04
(tmp / "ledger.json").write_text(json.dumps({"cycle": 317, "accepted": [{"claim": "x"}]}))
(tmp / "proposals").mkdir(); (tmp / "verifiers").mkdir(); (tmp / "objections").mkdir()
(tmp / "verifiers" / "c1.py").write_text("print(1)")

loop.bind_state("analyst")
ok.append(("analyst ledger moved", loop.LEDGER == tmp / "analyst/ledger.json" and loop.LEDGER.exists()))
ok.append(("cycle count survived", json.loads(loop.LEDGER.read_text())["cycle"] == 317))
ok.append(("verifier archive moved", (tmp / "analyst/verifiers/c1.py").exists()))
ok.append(("nothing left at the old path", not (tmp / "ledger.json").exists()))

loop.bind_state("burndown")
ok.append(("burndown gets its own", loop.LEDGER == tmp / "burndown/ledger.json" and not loop.LEDGER.exists()))
ok.append(("burndown did not inherit", not (tmp / "burndown/verifiers/c1.py").exists()))

# a second bind must not re-migrate or clobber
loop.bind_state("analyst")
ok.append(("migration is idempotent", json.loads(loop.LEDGER.read_text())["cycle"] == 317))

# the mutex
a = loop.hold_lock("analyst")
ok.append(("first agent takes the lock", a is not None))
ok.append(("second agent is refused", loop.hold_lock("burndown") is None))
a.close()
b = loop.hold_lock("burndown")
ok.append(("released, so the next one gets it", b is not None))
# and across processes, which is the case that matters
b.close()
c = loop.hold_lock("analyst")
r = subprocess.run([sys.executable, "-c",
                    f"import sys; sys.path.insert(0,'{Path(__file__).resolve().parents[1]}');"
                    f"import loop; loop.ROOT=__import__('pathlib').Path('{tmp}');"
                    f"loop.LOCK=loop.ROOT/'loop.lock';"
                    f"print('GOT' if loop.hold_lock('burndown') else 'REFUSED')"],
                   capture_output=True, text=True)
ok.append(("refused from another process", "REFUSED" in r.stdout))
c.close()

for name, good in ok:
    print(f"{'PASS' if good else 'FAIL'}  {name}")
print("\n%d/%d passed" % (sum(g for _, g in ok), len(ok)))
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if all(g for _, g in ok) else 1)
