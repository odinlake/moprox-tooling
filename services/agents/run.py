#!/usr/bin/env python3
"""Run a Claude Code agent headless on the subscription (Max plan), in its own CLAUDE.md context dir.

Each agent is just a directory with a CLAUDE.md. We strip any inference tokens from the env so it
uses the /login subscription creds (not the metered API). Used by the Polar fetcher (coach) and the
Telegram router (steward / coach / dev).
"""
import os, shutil, subprocess, sys
from pathlib import Path

HOME = Path.home()
AGENTS = {
    "coach":   HOME / "projects/private-data/agents/coach",
    "steward": HOME / "projects/private-data/agents/steward",
    "dev":     HOME / "projects/private-data/agents/dev",     # the persona dir (loads its CLAUDE.md)
}
REPOS = [HOME / "projects/moprox-homelab", HOME / "projects/moprox-tooling", HOME / "projects/private-data"]
BOOK  = HOME / ".local/share/moprox"                          # the book of works lives here
LOCAL_BIN = HOME / ".local/bin"
# Resolve the CLI absolutely: under systemd the service PATH is minimal and won't find ~/.local/bin.
CLAUDE = shutil.which("claude") or str(LOCAL_BIN / "claude")

# The dev agent acts on simple, reversible work (operator's chosen autonomy). Irreversible / outward /
# privileged commands are denied at the TOOL layer here; the persona handles the nuance + book-of-works.
DEV_DENY = ",".join("Bash(%s)" % p for p in (
    "git push:*", "git reset --hard:*", "git clean:*", "sudo:*", "rm:*",
    "reboot:*", "shutdown:*", "dd:*", "mkfs:*", "gh:*"))   # service mutation needs sudo (denied); read-only systemctl ok
TRAINING_DATA = HOME / ".cache/moprox-dashboard-ghpages/dashboard/data/training"   # classified history
CONVO_TOOL = "Bash(convo:*)"            # restricted: agents may run ONLY the `convo` helper, no free bash
AGENT_FLAGS = {
    # dev already has broad Bash (can run convo directly); the rest get the convo helper as their one
    # way to read/search the shared conversation on demand.
    "dev": ["--permission-mode", "acceptEdits",
            "--allowedTools", "Bash,Edit,Write,Read,Grep,Glob",
            "--disallowedTools", DEV_DENY,
            "--add-dir", str(REPOS[0]), str(REPOS[1]), str(REPOS[2]), str(BOOK)],  # variadic: keep last
    # coach: read-only session history + the convo helper (search/tail the conversation)
    "coach": ["--allowedTools", "Read,Grep,Glob,%s" % CONVO_TOOL, "--add-dir", str(TRAINING_DATA)],
    # steward: only the convo helper, to investigate routing history when answering meta questions
    "steward": ["--allowedTools", CONVO_TOOL],
}

def run_agent(agent, prompt, timeout=600):
    cwd = AGENTS[agent]
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")}
    env["PATH"] = "%s:%s" % (LOCAL_BIN, env.get("PATH", "/usr/bin:/bin"))   # claude shells out to node
    cmd = [CLAUDE, "-p", prompt] + AGENT_FLAGS.get(agent, [])               # prompt before variadic flags
    r = subprocess.run(cmd, cwd=str(cwd), env=env,
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("agent %s failed: %s" % (agent, (r.stderr or r.stdout)[:300]))
    return r.stdout.strip()

if __name__ == "__main__":
    print(run_agent(sys.argv[1], sys.argv[2]))
