#!/usr/bin/env python3
"""Run a Claude Code agent headless on the subscription (Max plan), in its own CLAUDE.md context dir.

Each agent is just a directory with a CLAUDE.md. We strip any inference tokens from the env so it
uses the /login subscription creds (not the metered API). Used by the Polar fetcher (coach) and the
Telegram router (steward / coach / dev).
"""
import json, os, shutil, subprocess, sys, time
from pathlib import Path

HOME = Path.home()
USAGE = HOME / ".local/share/moprox/agent-usage.jsonl"   # per-call token ledger (one line per invocation)
AGENTS = {
    "coach":   HOME / "projects/private-data/agents/coach",
    "steward": HOME / "projects/private-data/agents/steward",
    "dev":     HOME / "projects/private-data/agents/dev",     # the persona dir (loads its CLAUDE.md)
    "valet":   HOME / "projects/private-data/agents/valet",
    "theming": HOME / "projects/private-data/agents/theming",
}
REPOS = [HOME / "projects/moprox-homelab", HOME / "projects/moprox-tooling", HOME / "projects/private-data"]
BOOK  = HOME / ".local/share/moprox"                          # the book of works lives here
SHARED_MEM = HOME / ".claude/projects/-home-mikael/memory"    # the dev agent shares THIS session's memory
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
THEMING_REPO = HOME / "projects/theming"               # the theme-ontology/theming working copy
# theming pushes feature BRANCHES and opens PRs, but never bypasses protections: deny force-push +
# direct master/main pushes, the dangerous `gh` verbs (merge a PR, delete/archive a repo), and the
# usual catastrophic/outward commands. Plain `git push` of a feature branch and `gh pr create` ARE
# allowed; server-side branch protection on master/main is the real backstop for "no self-merge".
THEMING_DENY = ",".join("Bash(%s)" % p for p in (
    "git push --force:*", "git push -f:*", "git push *master*", "git push origin master:*",
    "git reset --hard:*", "git clean:*", "sudo:*", "rm:*", "reboot:*", "shutdown:*",
    "dd:*", "mkfs:*", "gh pr merge:*", "gh repo delete:*", "gh repo archive:*"))
AGENT_FLAGS = {
    # dev already has broad Bash (can run convo directly); the rest get the convo helper as their one
    # way to read/search the shared conversation on demand.
    "dev": ["--permission-mode", "acceptEdits",
            "--allowedTools", "Bash,Edit,Write,Read,Grep,Glob",
            "--disallowedTools", DEV_DENY,
            # shares THIS session's memory (also symlinked into the dev project dir so it auto-loads);
            "--add-dir", str(REPOS[0]), str(REPOS[1]), str(REPOS[2]), str(BOOK), str(SHARED_MEM)],  # variadic: keep last
    # coach: a real analyst — Python (matplotlib + the analysis engine), can build & SEND charts to
    # Telegram, do web research (WebSearch/WebFetch), edit its own memory. Catastrophic commands denied.
    "coach": ["--permission-mode", "acceptEdits",
              "--allowedTools", "Bash,Edit,Write,Read,Grep,Glob,WebSearch,WebFetch",
              "--disallowedTools", DEV_DENY,
              "--add-dir", str(REPOS[1]), str(REPOS[2]), str(TRAINING_DATA)],   # tooling + private-data + dash data
    # steward: only the convo helper, to investigate routing history when answering meta questions
    "steward": ["--allowedTools", CONVO_TOOL],
    # valet: writes its own preference memory (learns what to surface) + the convo helper
    # valet: morning brief + afternoon catch-up; reads (only) the Google Workspace MCP (read-only,
    # impersonating Mikael) for overnight email + calendar, scoped to ONLY this agent.
    "valet": ["--permission-mode", "acceptEdits",
              "--allowedTools", "Read,Grep,Glob,Edit,Write,mcp__google,%s" % CONVO_TOOL,
              "--mcp-config", str(AGENTS["valet"] / "mcp.json"), "--strict-mcp-config"],
    # theming: theme-ontology expert. Answers data questions via the totolo MCP (scoped to ONLY this
    # agent with --mcp-config + --strict-mcp-config) and prepares branch-only edits to the theming repo.
    "theming": ["--permission-mode", "acceptEdits",
                "--allowedTools",
                "Bash,Edit,Write,Read,Grep,Glob,WebSearch,WebFetch,mcp__totolo",   # all totolo MCP tools
                "--disallowedTools", THEMING_DENY,
                "--mcp-config", str(AGENTS["theming"] / "mcp.json"), "--strict-mcp-config",
                "--add-dir", str(THEMING_REPO)],   # variadic: keep last
}

def _log_usage(agent, j):
    """Append one line of token accounting from the --output-format json result envelope (or an
    {"_error": ...} marker on failure)."""
    u = j.get("usage") or {}
    rec = {"ts": int(time.time()), "agent": agent,
           "in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0),
           "cache_read": u.get("cache_read_input_tokens", 0),
           "cache_write": u.get("cache_creation_input_tokens", 0),
           "cost_usd": j.get("total_cost_usd"), "ms": j.get("duration_ms"),
           "turns": j.get("num_turns"), "error": j.get("_error")}
    try:
        USAGE.parent.mkdir(parents=True, exist_ok=True)
        with open(USAGE, "a") as f: f.write(json.dumps(rec) + "\n")
    except Exception:
        pass
    return rec

def run_agent(agent, prompt, timeout=600):
    cwd = AGENTS[agent]
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")}
    env["PATH"] = "%s:%s" % (LOCAL_BIN, env.get("PATH", "/usr/bin:/bin"))   # claude shells out to node
    cmd = [CLAUDE, "-p", prompt, "--output-format", "json"] + AGENT_FLAGS.get(agent, [])
    try:
        r = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _log_usage(agent, {"_error": "timeout"})
        raise RuntimeError("timed out after %ds — too big a task to finish in one go" % timeout)
    if r.returncode != 0:
        _log_usage(agent, {"_error": "exit %d" % r.returncode})
        raise RuntimeError("agent %s failed: %s" % (agent, (r.stderr or r.stdout)[:300]))
    try:                                              # json envelope: {result, usage, total_cost_usd, ...}
        j = json.loads(r.stdout)
        _log_usage(agent, j)
        return (j.get("result") or "").strip()
    except (json.JSONDecodeError, TypeError):         # fall back to raw text if the format ever changes
        return r.stdout.strip()

if __name__ == "__main__":
    print(run_agent(sys.argv[1], sys.argv[2]))
