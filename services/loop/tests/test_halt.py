#!/usr/bin/env python3
"""The strike gate: said once, probed every HALT_RETRY_H, never a latch.

    python3 services/loop/tests/test_halt.py
"""
import os, sys
from pathlib import Path
os.environ["LOOP_ADVERSARIAL"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import loop

H = loop.HALT_RETRY_H * 3600
led = {"strikes": 0}
assert loop.halt_gate(led, 1000) == "run" and "halted_at" not in led
led = {"strikes": loop.MAX_STRIKES, "inflight": "cycle 946 (stuck)"}
assert loop.halt_gate(led, 1000) == "first" and led["halted_at"] == 1000
assert loop.halt_gate(led, 1000 + 3600) == "quiet"            # the hourly wake-ups say nothing new
assert led["strikes"] == loop.MAX_STRIKES and led["inflight"]
assert loop.halt_gate(led, 1000 + H + 1) == "probe"
# the probe leaves the saved ledger STILL halted, stamped now: main() saves it before the agent
# runs, so a probe killed outright (oom-kill) must not turn every later wake into a bare "run"
assert led["strikes"] == loop.MAX_STRIKES and led["inflight"] is None
assert led["halted_at"] == 1000 + H + 1
assert loop.halt_gate(led, 1000 + H + 2) == "quiet"           # a killed probe: wait an interval
assert loop.halt_gate(led, 1000 + 2 * H + 1) == "probe"       # ... then one more fresh cycle
# 3 days of the real timer grid (07..23:00 hourly) from a fresh halt, every probe killed:
led, launched = {"strikes": loop.MAX_STRIKES}, 0
for d in range(3):
    for h in range(7, 24):
        launched += loop.halt_gate(led, 1.8e9 + d * 86400 + (h - 7) * 3600) in ("run", "probe")
assert launched <= 3 * 24 / loop.HALT_RETRY_H + 1, launched   # a cycle per interval, not per wake
# a failed probe (main() bumps strikes and restarts the clock) is quiet for a whole interval again
led = {"strikes": loop.MAX_STRIKES, "halted_at": 1000}
assert loop.halt_gate(led, 1000 + H + 1) == "probe"
loop.take_strike(led, now=5000)                               # the probe came back bad
assert led["strikes"] == loop.MAX_STRIKES, led["strikes"]     # "strike 3/3", not 4/3
assert led["halted_at"] == 5000
assert loop.halt_gate(led, 5000 + H - 1) == "quiet"
assert loop.halt_gate(led, 5000 + H + 1) == "probe"
# not halted: a bad cycle counts up to the threshold, and the gate then halts
led = {"strikes": 0}
for _ in range(loop.MAX_STRIKES):
    assert loop.halt_gate(led, 100) == "run"
    loop.take_strike(led, now=100)
assert led["strikes"] == loop.MAX_STRIKES and "halted_at" not in led
assert loop.halt_gate(led, 100) == "first"
print("ok: halt gate")
