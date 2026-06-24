#!/usr/bin/env python3
"""Run a Claude Code agent headless on the subscription (Max plan), in its own CLAUDE.md context dir.

Each agent is just a directory with a CLAUDE.md. We strip any inference tokens from the env so it
uses the /login subscription creds (not the metered API). Used by the Polar fetcher (coach) and the
Telegram router (steward / coach / dev).
"""
import os, subprocess, sys
from pathlib import Path

AGENTS = {
    "coach":   Path.home() / "projects/private-data/agents/coach",
    "steward": Path.home() / "projects/private-data/agents/steward",
    "dev":     Path.home() / "projects/moprox-homelab",
}

def run_agent(agent, prompt, timeout=600):
    cwd = AGENTS[agent]
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")}
    r = subprocess.run(["claude", "-p", prompt], cwd=str(cwd), env=env,
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("agent %s failed: %s" % (agent, (r.stderr or r.stdout)[:300]))
    return r.stdout.strip()

if __name__ == "__main__":
    print(run_agent(sys.argv[1], sys.argv[2]))
