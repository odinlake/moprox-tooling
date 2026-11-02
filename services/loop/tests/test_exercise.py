#!/usr/bin/env python3
"""The tool-exercise rotation: who is due, and what a report does to the ledger.

    python3 services/loop/tests/test_exercise.py

No network: mcp_inventory is replaced with a fixed list, and err/notify are captured.
"""
import sys, json, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import loop

tmp = Path(tempfile.mkdtemp(prefix="exercise-"))
loop.STATE = tmp
errs, sent = [], []
loop.err = lambda msg, exc=None: errs.append(msg)
loop.warn = lambda msg: errs.append("warn:" + msg)
loop.notify.send = lambda msg, agent="": sent.append(msg)
loop.say = lambda *a, **k: None
TOOLS = {f"mcp__s__t{i}": f"tool {i}" for i in range(5)}
loop.mcp_inventory = lambda caps: (dict(TOOLS), [])
ok = []

led = {"exercised": {"mcp__s__t0": {"at": 100, "ok": True}, "mcp__s__t1": {"at": 50, "ok": True}}}
loop.EXERCISE_N = 3
block = loop.exercise_block(led, "burndown")
ok.append(("never-exercised come first, then oldest",
           led["exercise_due"] == ["mcp__s__t2", "mcp__s__t3", "mcp__s__t4"]))
ok.append(("block names the report file", str(tmp / "exercise.json") in block))

(tmp / "exercise.json").write_text(json.dumps([
    {"tool": "mcp__s__t2", "ok": True, "note": "searched, sane"},
    {"tool": "mcp__s__t3", "ok": False, "note": "Argument list too long"}]))
loop.record_exercise(led, 7, "burndown")
ex = led["exercised"]
ok.append(("ok stamped", ex["mcp__s__t2"]["ok"] is True and ex["mcp__s__t2"]["cycle"] == 7))
ok.append(("failure stamped False", ex["mcp__s__t3"]["ok"] is False))
ok.append(("failure reaches err", any("t3 FAILED" in e for e in errs)))
ok.append(("failure notified", any("TOOL BROKEN" in m for m in sent)))
ok.append(("unreported stays due (not stamped)", "mcp__s__t4" not in ex))
ok.append(("unreported warned", any("warn:" in e and "t4" in e for e in errs)))
ok.append(("report consumed", not (tmp / "exercise.json").exists()))
ok.append(("due cleared", "exercise_due" not in led))

loop.exercise_block(led, "burndown")
ok.append(("next cycle picks the unreported one first", led["exercise_due"][0] == "mcp__s__t4"))
(tmp / "exercise.json").write_text("{not json")
errs.clear(); loop.record_exercise(led, 8, "burndown")
ok.append(("garbage report is an err, nothing stamped",
           any("unreadable" in e for e in errs) and ex.get("mcp__s__t4") is None))

caps = json.loads(loop.CAPS.read_text())
ok.append(("hold and how never overlap", not set(caps["exercise"]["hold"]) & set(caps["exercise"]["how"])))

for name, passed in ok:
    print(("PASS " if passed else "FAIL ") + name)
sys.exit(0 if all(p for _, p in ok) else 1)
