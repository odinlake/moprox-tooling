#!/usr/bin/env python3
"""Per-agent token accounting from the ledger run.py writes (~/.local/share/moprox/agent-usage.jsonl).

Each agent is a fresh, stateless `claude -p`, so its context does NOT grow across calls — it's roughly
constant: system prompt + its CLAUDE.md + @-imported memory + the prompt handed in. So the **average
input tokens per call** is effectively that agent's standing context size; `cache%` is how much of it
is served from cache (the stable CLAUDE.md/memory). `$est` is the metered-API equivalent cost the CLI
reports — NOT actually billed (everything runs on the Max subscription), useful only for relative weight.

  usage.py            # per-agent summary
  usage.py <agent>    # recent individual calls for one agent
"""
import json, sys, time
from pathlib import Path
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1] / "lib"))
import errlog  # noqa: E402  — no silent swallows; see services/lib/errlog.py

import ledger   # local ledger + the ledgers pulled from other hosts, agent names normalised

def rows():
    return ledger.rows()

def _n(r, k):
    """A token count, defensively. `r.get(k, 0)` returns None when the key EXISTS with a null value
    — the default only covers a MISSING key — so three error rows (in=None, written when a call
    never produced a usage envelope) crashed this whole summary. agent_stats.py already guards the
    same way; counted via errlog, not swallowed."""
    v = r.get(k)
    if isinstance(v, (int, float)): return v
    if v is None and k in r:
        errlog.skip("usage.py: null token field in usage ledger", ValueError("%s=None" % k))
    return 0

def summary():
    by = {}
    for r in rows():
        d = by.setdefault(r.get("agent", "?"), {"n": 0, "in": 0, "cr": 0, "cw": 0, "out": 0, "cost": 0.0})
        d["n"] += 1; d["in"] += _n(r, "in"); d["cr"] += _n(r, "cache_read")
        d["cw"] += _n(r, "cache_write"); d["out"] += _n(r, "out"); d["cost"] += r.get("cost_usd") or 0.0
    if not by:
        print("no agent calls logged yet"); return
    print("%-9s %6s %14s %12s %7s %9s" % ("agent", "calls", "ctx(avg tok)", "out(total)", "cache", "$est"))
    for a, d in sorted(by.items()):
        ctx = d["in"] + d["cr"] + d["cw"]                 # full prompt size = new + cache-read + cache-write
        avg = ctx // max(d["n"], 1)
        cache = 100 * d["cr"] // max(ctx, 1)
        print("%-9s %6d %14d %12d %6d%% %9.4f" % (a, d["n"], avg, d["out"], cache, d["cost"]))
    print("\nctx(avg tok) = full input context per query (new + cache_read + cache_write); it's the agent's")
    print("standing CLAUDE.md + memory + prompt, ~constant since each call is a fresh stateless process.")
    print("cache% = share served from cache. $est is metered-equivalent, NOT billed (Max subscription).")

def detail(agent):
    for r in rows():
        if r.get("agent") == agent:
            print("%s  in=%-6d out=%-5d cache_read=%-6d %sms $%s" % (
                time.strftime("%m-%d %H:%M", time.localtime(r.get("ts", 0))),
                r.get("in", 0), r.get("out", 0), r.get("cache_read", 0), r.get("ms"), r.get("cost_usd")))

if __name__ == "__main__":
    detail(sys.argv[1]) if len(sys.argv) > 1 else summary()
