#!/usr/bin/env python3
"""Aggregate the agent usage ledger (+ daily statements) into dashboard data for the Stats tab.

Two windows (24h, 30d); one row per agent: its latest statement, invocation count, total tokens
burned (input + output + cache read/write), cache hit-rate, average latency, and failures. Writes
$OUT (the dashboard's data/stats/agents.json).
"""
import json, os, time
from pathlib import Path

LEDGER = Path.home() / ".local/share/moprox/agent-usage.jsonl"
STMT   = Path.home() / ".local/share/moprox/agent-statements.json"
AGENTS = ["coach", "steward", "dev", "valet", "theming"]

def _jsonl(p):
    if not p.exists(): return []
    out = []
    for ln in p.read_text().splitlines():
        try: out.append(json.loads(ln))
        except Exception: pass
    return out

def window(rows, secs, stmts):
    cut = time.time() - secs
    by = {}
    for r in rows:
        if r.get("ts", 0) < cut: continue
        d = by.setdefault(r.get("agent", "?"), {"calls": 0, "tok": 0, "cr": 0, "ctx": 0, "ms": 0, "fails": 0})
        d["calls"] += 1
        if r.get("error"):
            d["fails"] += 1; continue
        d["tok"] += r.get("in", 0) + r.get("out", 0) + r.get("cache_read", 0) + r.get("cache_write", 0)
        d["cr"] += r.get("cache_read", 0)
        d["ctx"] += r.get("in", 0) + r.get("cache_read", 0) + r.get("cache_write", 0)
        d["ms"] += r.get("ms") or 0
    out = []
    for a in AGENTS:
        d = by.get(a, {"calls": 0, "tok": 0, "cr": 0, "ctx": 0, "ms": 0, "fails": 0})
        ok = max(d["calls"] - d["fails"], 1)
        out.append({"agent": a, "statement": (stmts.get(a) or {}).get("text", ""),
                    "calls": d["calls"], "tokens": d["tok"],
                    "cache_pct": round(100 * d["cr"] / max(d["ctx"], 1)),
                    "avg_ms": round(d["ms"] / ok), "fails": d["fails"]})
    return out

def _icons():
    out = {}
    for a in AGENTS:
        try: out[a] = (Path.home() / ("projects/private-data/agents/%s/icon.svg" % a)).read_text().strip()
        except Exception: pass
    return out

def main():
    rows = _jsonl(LEDGER)
    stmts = json.loads(STMT.read_text()) if STMT.exists() else {}
    data = {"generated": int(time.time()), "icons": _icons(),
            "windows": {"24h": window(rows, 86400, stmts), "30d": window(rows, 30 * 86400, stmts)}}
    out = Path(os.environ.get("OUT", "agents.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, separators=(",", ":")))
    print("wrote", out)

if __name__ == "__main__":
    main()
