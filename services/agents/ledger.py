#!/usr/bin/env python3
"""The agent usage ledger, as its readers should see it: every host's rows, one name per agent.

Two things made the Stats page lie about the analyst, and both are fixed here rather than in each
reader (measured 2026-08-17: the page showed 11 calls / 279k tokens — the daily statement calls —
against 169.2M tokens of real loop work):

  1. **Wrong host.** The loop runs on claude-loop and writes ITS ledger there, at the same path.
     `usage_sync.py` pulls those files here as `agent-usage-<host>.jsonl`; this module reads the
     local ledger plus every sibling, so a reader gets the fleet, not one box.
  2. **Wrong name.** `loop.py` writes `loop-analyst`, `run.py` writes `analyst`. Same agent, two
     rows — and `agent_stats.py` emits only names in run.py's roster, so the loop rows were dropped
     even when present. The `loop-` prefix is folded away and kept as `via` for greppability.
"""
import json
from pathlib import Path
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1] / "lib"))
import errlog  # noqa: E402  — no silent swallows; see services/lib/errlog.py

D = Path.home() / ".local/share/moprox"
LEDGER = D / "agent-usage.jsonl"                    # written here by run.py (and loop.py, on the loop guest)
PULLED = "agent-usage-*.jsonl"                      # ledgers pulled from other hosts by usage_sync.py


def files():
    return [p for p in [LEDGER] + sorted(D.glob(PULLED)) if p.exists()]


def _read(p, via):
    out = []
    for ln in p.read_text().splitlines():
        try: r = json.loads(ln)
        except Exception as e:
            errlog.skip("ledger.py: %s line" % p.name, e)
            continue
        a = r.get("agent") or "?"
        if a.startswith("loop-"):                   # the loop harness IS that agent, not another one
            r["agent"], r["via"] = a[5:], "loop"
        if via: r.setdefault("host", via)
        out.append(r)
    return out


def rows():
    """Every ledger row on this box, agent names normalised, oldest file first."""
    out = []
    for p in files():
        via = p.name[len("agent-usage-"):-len(".jsonl")] if p.name != LEDGER.name else None
        out.extend(_read(p, via))
    out.sort(key=lambda r: r.get("ts") or 0)
    return out
