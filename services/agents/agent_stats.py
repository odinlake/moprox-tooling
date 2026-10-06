#!/usr/bin/env python3
"""Aggregate the agent usage ledger (+ daily statements) into dashboard data for the Stats tab.

Two windows (24h, 30d); one row per agent: its latest statement, invocation count, total tokens
burned (input + output + cache read/write), cache hit-rate, average latency, and failures. Writes
$OUT (the dashboard's data/stats/agents.json).
"""
import json, os, time
from pathlib import Path
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1] / "lib"))
import errlog  # noqa: E402  — no silent swallows; see services/lib/errlog.py

import ledger   # local + pulled-from-other-hosts rows, `loop-analyst` folded into `analyst`
STMT   = Path.home() / ".local/share/moprox/agent-statements.json"
# Roster comes from run.py so it cannot drift from the one that actually invokes agents...
from run import AGENTS as _A
AGENT_DIRS = dict(_A)
# ...but run.py is not the only thing that invokes agents, and this panel reports the LEDGER. The
# loop harness (loop@<name>) writes its own rows and never touches run.py's table, so `burndown`
# burned 295.5M tokens over 30d — 32.6% of the panel's own total — with no row to put them in and
# no hint that anything was missing (measured 2026-10-06, cycle 577; the same class of silent drop
# ledger.py's docstring records for `loop-analyst`, which was fixed by name and not by roster).
# So: run.py's table UNION whatever the ledger actually contains. A new loop agent now appears on
# the panel the first time it burns a token, instead of waiting for someone to notice it is absent.
LEDGER_AGENTS = Path.home() / "projects/private-data/agents"   # icon home for agents run.py never launches


def roster(rows, secs=30 * 86400):
    """run.py's table, plus every agent the ledger saw inside the widest window on the panel.

    Windowed, not all-history: an agent that is retired (or a name typed wrong once) would
    otherwise keep a permanent zero row long after its last call. It ages out when its burn does.
    """
    cut = time.time() - secs
    seen = {r.get("agent") for r in rows if r.get("agent") and (r.get("ts") or 0) >= cut}
    return sorted(set(AGENT_DIRS) | seen)

def _n(r, k):
    """Token counts, defensively. `r.get(k, 0)` returns None when the key EXISTS with a null value —
    the default only covers a MISSING key — so a single null row crashed the whole panel with
    "unsupported operand type(s) for +: NoneType and NoneType". Counted, not swallowed."""
    v = r.get(k)
    if isinstance(v, (int, float)):
        return v
    if v is None and k in r:
        errlog.skip("agent_stats.py: null token field in usage ledger", ValueError(f"{k}=None"))
    return 0


def window(rows, secs, stmts, agents):
    cut = time.time() - secs
    by = {}
    for r in rows:
        if r.get("ts", 0) < cut: continue
        d = by.setdefault(r.get("agent", "?"), {"calls": 0, "tok": 0, "cr": 0, "ctx": 0, "ms": 0, "fails": 0})
        d["calls"] += 1
        if r.get("error"):
            d["fails"] += 1; continue
        d["tok"] += _n(r, "in") + _n(r, "out") + _n(r, "cache_read") + _n(r, "cache_write")
        d["cr"] += _n(r, "cache_read")
        d["ctx"] += _n(r, "in") + _n(r, "cache_read") + _n(r, "cache_write")
        d["ms"] += r.get("ms") or 0
    out = []
    for a in agents:
        d = by.get(a, {"calls": 0, "tok": 0, "cr": 0, "ctx": 0, "ms": 0, "fails": 0})
        ok = max(d["calls"] - d["fails"], 1)
        out.append({"agent": a, "statement": (stmts.get(a) or {}).get("text", ""),
                    "calls": d["calls"], "tokens": d["tok"],
                    "cache_pct": round(100 * d["cr"] / max(d["ctx"], 1)),
                    "avg_ms": round(d["ms"] / ok), "fails": d["fails"]})
    return out

def _icons(agents):
    out = {}
    for a in agents:
        # The KEY is not the directory: `bard-curate` is the bard persona under a second set of
        # permissions, so keying the path off the name looked for agents/bard-curate/ and warned
        # every run, forever, about a directory that is never going to exist. Go through run.py's
        # own table, which already says where each agent lives.
        try: out[a] = (AGENT_DIRS.get(a) or LEDGER_AGENTS / a).joinpath("icon.svg").read_text().strip()
        except Exception as _e:
            # total= so that losing EVERY icon (private-data unmounted, the agents/ dir renamed)
            # reports at err instead of being one digit away from the routine two-missing warning.
            errlog.skip("agent_stats.py: agent icon", _e, total=len(agents))
            pass
    return out

def main():
    rows = ledger.rows()
    stmts = json.loads(STMT.read_text()) if STMT.exists() else {}
    agents = roster(rows)
    data = {"generated": int(time.time()), "icons": _icons(agents),
            "windows": {"24h": window(rows, 86400, stmts, agents),
                        "30d": window(rows, 30 * 86400, stmts, agents)}}
    out = Path(os.environ.get("OUT", "agents.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, separators=(",", ":")))
    print("wrote", out)

if __name__ == "__main__":
    main()
