#!/usr/bin/env python3
"""Aggregate MCP tool-call usage per agent × endpoint from the agents' Claude Code transcripts,
over rolling windows, for the dashboard. Each agent runs in its own cwd, so its transcripts live in
~/.claude/projects/-home-mikael-projects-private-data-agents-<agent>/*.jsonl — i.e. already labelled
by agent. We scan those for tool_use blocks named mcp__<server>__<tool> and bucket by time window.
Writes $OUT (data/stats/mcp.json). No change to the agent runner needed — works retroactively."""
import json, os, glob, time, datetime
from pathlib import Path

PROJ = Path.home() / ".claude/projects"
GLOB = str(PROJ / "-home-mikael-projects-private-data-agents-*")
OUT  = Path(os.environ.get("OUT", "mcp.json"))
WINDOWS = {"24h": 86400, "7d": 7*86400, "30d": 30*86400}
MAXSEC = max(WINDOWS.values())

def parse_ts(s):
    if not s: return None
    try: return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception: return None

def main():
    now = time.time(); cutoff = now - MAXSEC
    calls = []   # (agent, endpoint, ts)
    for d in glob.glob(GLOB):
        agent = d.split("agents-")[-1]
        for f in glob.glob(d + "/*.jsonl"):
            try:
                if os.path.getmtime(f) < cutoff: continue
            except OSError: continue
            for ln in open(f, errors="ignore"):
                if "mcp__" not in ln: continue
                try: r = json.loads(ln)
                except Exception: continue
                cont = (r.get("message") or {}).get("content")
                if not isinstance(cont, list): continue
                ts = parse_ts(r.get("timestamp"))
                if ts is None or ts < cutoff: continue
                for c in cont:
                    if isinstance(c, dict) and c.get("type") == "tool_use" and str(c.get("name", "")).startswith("mcp__"):
                        p = c["name"].split("__")
                        ep = "%s·%s" % (p[1], "__".join(p[2:])) if len(p) >= 3 else c["name"]
                        calls.append((agent, ep, ts))
    data = {}
    for w, secs in WINDOWS.items():
        lo = now - secs
        by_agent, agent_tot, ep_tot = {}, {}, {}
        for a, ep, ts in calls:
            if ts < lo: continue
            by_agent.setdefault(a, {}); by_agent[a][ep] = by_agent[a].get(ep, 0) + 1
            agent_tot[a] = agent_tot.get(a, 0) + 1
            ep_tot[ep] = ep_tot.get(ep, 0) + 1
        data[w] = {"by_agent": by_agent, "agent_tot": agent_tot, "ep_tot": ep_tot,
                   "total": sum(agent_tot.values())}
    out = {"generated": int(now), "windows": list(WINDOWS), "data": data}
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    print("wrote %s: %d mcp calls; 24h=%d 7d=%d 30d=%d" %
          (OUT, len(calls), data["24h"]["total"], data["7d"]["total"], data["30d"]["total"]))

if __name__ == "__main__":
    main()
