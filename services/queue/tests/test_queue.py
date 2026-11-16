#!/usr/bin/env python3
"""The queue runner's gates, with no agent and no board.

    python3 services/queue/tests/test_queue.py
"""
import json, os, sys, tempfile, time
from pathlib import Path
tmp = Path(tempfile.mkdtemp(prefix="queue-"))
os.environ["QUEUE_STATE_DIR"] = str(tmp)
os.environ["QUEUE_LOCAL_CONF"] = str(tmp / "local.json")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import queue_run as q

c = q.conf("planner")
assert q.model_flags(c) == ["--model", c["model"], "--effort", c["effort"]]
assert q.model_flags({"model": "", "effort": ""}) == []          # empty = the CLI default
(tmp / "local.json").write_text(json.dumps({"planner": {"model": "next-best", "effort": ""}}))
assert q.model_flags(q.conf("planner")) == ["--model", "next-best"]   # a host override, no commit
(tmp / "local.json").unlink()

now, H = 1_000_000.0, 3600
item = lambda id, upd="u1", urgent=False: {"id": id, "updated": upd, "urgent": urgent}
due = lambda items, st: q.planner_due(items, st, c, now)
assert due([item("a")], {"runs": [], "seen": {}})[0]                      # new item, never run
assert not due([item("a")], {"runs": [now - H], "seen": {}})[0]           # inside the gap
assert due([item("a", urgent=True)], {"runs": [now - H], "seen": {}})[0]  # urgent jumps the gap
assert not due([item("a", urgent=True)], {"runs": [now - 60], "seen": {}})[0]
many = [now - (i + 1) * 7 * H / 4 for i in range(3)]
assert not due([item("a")], {"runs": many, "seen": {}})[0]                # daily cap
assert due([item("a", urgent=True)], {"runs": many, "seen": {}})[0]       # urgent has its own cap
# an item the planner kept is not re-read until it changes, or a day passes
assert not due([item("a")], {"runs": [now - 8 * H], "seen": {"a": "u1"}})[0]
assert due([item("a", "u2")], {"runs": [now - 8 * H], "seen": {"a": "u1"}})[0]
assert due([item("a")], {"runs": [now - 25 * H], "seen": {"a": "u1"}})[0]
assert not due([item("a", urgent=True)], {"runs": [now - 2 * H], "seen": {"a": "u1"}})[0]  # urgent, already seen

# dev: two failures or stale days go back to the planner, never anywhere else
from datetime import datetime, timezone
iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
t = time.time()
rows = [{"id": "fresh", "owner": "dev", "owner_since": iso(t - 3600)},
        {"id": "stale", "owner": "dev", "owner_since": iso(t - 6 * 86400)},
        {"id": "failed", "owner": "dev", "owner_since": iso(t - 3600)},
        {"id": "his", "owner": "operator"}]
q.board = lambda: rows
sent = []
q.write = lambda **f: sent.append(f) or {"issue": f}
q.save("dev", {"runs": [t] * 99, "attempts": {"failed": 2, "gone": 1}})   # run cap hit: no agent
assert q.run_dev(q.conf("dev"), False, False) == 0
assert sorted(f["id"] for f in sent) == ["failed", "stale"] and all(f["owner"] == "planner" for f in sent)
assert q.load("dev")["attempts"] == {}
print("ok: queue gates")
