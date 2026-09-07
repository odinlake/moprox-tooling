#!/usr/bin/env python3
"""Once a day, ask each agent for a terse status line in its own voice — a bit of personality for the
dashboard's Agents panel. Stored to agent-statements.json (read by agent_stats.py). Run on a timer.
"""
import json, re, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import errlog  # noqa: E402  — no silent swallows; see services/lib/errlog.py
# siblings from THIS tree, not ~/projects — see the note in services/forward/polar_fetch.py
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "services/agents"))
from run import run_agent, AGENTS  # noqa: E402

OUT = Path.home() / ".local/share/moprox/agent-statements.json"
PROMPT = ("In ONE short sentence (max ~15 words), a status line in your own voice — a wry or "
          "characteristic remark on your work or mood today. No preamble, no quotes, just the line.")

def main():
    data = json.loads(OUT.read_text()) if OUT.exists() else {}
    # One agent failing is a flake and stays a warning; ALL of them failing is one systemic fault —
    # a dead credential, a missing CLI — and has to reach the journal at err. On 2026-09-07 an
    # expired OAuth session lost all six here and this unit still exited 0, so the estate's queue
    # saw the same outage as three unrelated incidents plus one silence.
    skips = errlog.Skips("agent-statements: asking each agent for its status line")
    for a in AGENTS:
        try:
            s = run_agent(a, PROMPT, timeout=180).strip().splitlines()[0]
            s = re.sub(r"^#\w+", "", s).strip(" \t\"'—–-:")            # drop a leading #handle / dashes / quotes
            data[a] = {"text": s[:160], "ts": int(time.time())}
            print(a, "->", s[:70])
        except Exception as e:
            skips.add(e)
            print(a, "err", e)
    skips.report(total=len(AGENTS))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data))

if __name__ == "__main__":
    main()
