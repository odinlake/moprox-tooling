#!/usr/bin/env python3
"""houseip's decisions without the network or Telegram.

    python3 services/houseip/test_houseip.py
"""
import datetime, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import houseip as h  # noqa: E402

T0 = datetime.datetime(2026, 10, 28, 9, 0, tzinfo=datetime.timezone.utc)
m = lambda n: T0 + datetime.timedelta(minutes=n)
A, B = "86.144.178.229", "86.185.6.154"

st, msgs, ch = h.step({}, A, A, m(0))
assert st["ip"] == A and msgs == [] and not ch, "first run only records"

st, msgs, ch = h.step(st, B, A, m(5))
assert ch and len(msgs) == 1 and A in msgs[0] and B in msgs[0], "change pings once"

st, msgs, _ = h.step(st, B, A, m(10))
assert msgs == [], "stale DDNS gets STALE_AFTER grace"
st, msgs, _ = h.step(st, B, None, m(20))
assert msgs == [], "unresolvable DDNS says nothing"
st, msgs, _ = h.step(st, B, A, m(25))
assert len(msgs) == 1 and "won't connect" in msgs[0], "stale past grace alerts"
st, msgs, _ = h.step(st, B, A, m(30))
assert msgs == [], "stale alert is sent once"

st, msgs, _ = h.step(st, B, B, m(35))
assert len(msgs) == 1 and "again" in msgs[0] and "stale_since" not in st, "recovery is announced"
st, msgs, _ = h.step(st, B, B, m(40))
assert msgs == [], "steady state is silent"
print("ok")
