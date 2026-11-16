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
assert led["strikes"] == loop.MAX_STRIKES - 1 and led["inflight"] is None
assert loop.halt_gate(led, 1000 + H + 2) == "run"             # the probe cycle itself proceeds
# a failed probe (main() bumps strikes and restarts the clock) is quiet for a whole interval again
led["strikes"] = loop.MAX_STRIKES; led["halted_at"] = 5000
assert loop.halt_gate(led, 5000 + H - 1) == "quiet"
print("ok: halt gate")
