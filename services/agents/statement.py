#!/usr/bin/env python3
"""Once a day, ask each agent for a terse status line in its own voice — a bit of personality for the
dashboard's Agents panel. Stored to agent-statements.json (read by agent_stats.py). Run on a timer.
"""
import json, re, sys, time
from pathlib import Path
sys.path.insert(0, str(Path.home() / "projects/moprox-tooling/services/agents"))
from run import run_agent, AGENTS

OUT = Path.home() / ".local/share/moprox/agent-statements.json"
PROMPT = ("In ONE short sentence (max ~15 words), a status line in your own voice — a wry or "
          "characteristic remark on your work or mood today. No preamble, no quotes, just the line.")

def main():
    data = json.loads(OUT.read_text()) if OUT.exists() else {}
    for a in AGENTS:
        try:
            s = run_agent(a, PROMPT, timeout=180).strip().splitlines()[0]
            s = re.sub(r"^#\w+", "", s).strip(" \t\"'—–-:")            # drop a leading #handle / dashes / quotes
            data[a] = {"text": s[:160], "ts": int(time.time())}
            print(a, "->", s[:70])
        except Exception as e:
            print(a, "err", e)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data))

if __name__ == "__main__":
    main()
