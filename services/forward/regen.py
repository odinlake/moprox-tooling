#!/usr/bin/env python3
"""regen: let a Telegram agent re-run anything that has posted in its name.

Operator, 2026-11-20: "agents addressed on telegram should generally be able to regenerate anything
that was posted in their name on telegram". The trigger was valet refusing "regenerate the crash risk
gauges" at a permission prompt: the gauges are posted as #valet by market-chart.service, but valet
had no way to start that unit.

Which units an agent may start is DERIVED, never hand-listed, from two sources:
  1. the conversation log: since this change convo.log_out() records the systemd unit that sent
     each post (from /proc/self/cgroup), so any unit that has ever posted as #<agent> qualifies;
  2. a static scan, for history from before (1) existed: every installed *.service whose ExecStart
     runs a script in the production tree that posts with agent="<agent>".
The agent can start exactly those units and nothing else, so the grant is `Bash(regen:*)` and
needs no sudo rule of its own: this script runs `sudo -n systemctl start` itself, after the check.

    regen list              the units this agent may re-run, with when each last posted
    regen run <unit>        start it, wait for it, print the result and the tail of its journal

The calling agent comes from MOPROX_AGENT, which services/agents/run.py sets for every agent run.
"""
import json, os, re, subprocess, sys, time
from pathlib import Path

D = Path.home() / ".local/share/moprox"
LOGS = [D / "conversation-archive.jsonl", D / "conversation.jsonl"]
UNIT_DIRS = [Path("/etc/systemd/system")]
PROD = Path(os.environ.get("MOPROX_PROD", "/opt/moprox-tooling"))
# Units that post as an agent but are the agent's own runtime, not a job: re-running the Telegram
# poller or a live session would not regenerate anything, it would restart the conversation.
EXCLUDE = re.compile(r"^(telegram_poll|telegram-poll|dispatcher|discord-theming|moprox-dev@.*|moprox-axis|moprox-axis-count)\.service$")


def handle(agent):
    """bard-curate, bard-books post as #bard."""
    return (agent or "").split("-")[0]


def from_log(agent):
    """unit -> last ts it posted as this agent."""
    out = {}
    for p in LOGS:
        if not p.exists():
            continue
        with open(p) as f:
            for ln in f:
                if '"unit"' not in ln:
                    continue
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if r.get("dir") == "out" and handle(r.get("from")) == agent and r.get("unit"):
                    out[r["unit"]] = max(out.get(r["unit"], 0), r.get("ts", 0))
    return out


def from_units(agent):
    """Installed units whose ExecStart script posts as this agent."""
    pat = re.compile(r"""\bagent\s*=\s*["']%s["']|["']#%s\b""" % (re.escape(agent), re.escape(agent)),
                     re.IGNORECASE)          # agent="coach", AGENT = "coach", "#coach ..."
    out = set()
    for d in UNIT_DIRS:
        for u in d.glob("*.service"):
            try:
                text = u.read_text()
            except OSError:
                continue
            for line in text.splitlines():
                if not line.startswith("ExecStart="):
                    continue
                for tok in line.split():
                    if tok.startswith(str(PROD)) and tok.endswith((".py", ".sh")):
                        try:
                            if pat.search(Path(tok).read_text()):
                                out.add(u.name)
                        except OSError:
                            pass        # a unit pointing at a missing script is not ours to judge
    return out


def allowed(agent):
    seen = from_log(agent)
    units = {u: seen.get(u) for u in set(seen) | from_units(agent)}
    return {u: t for u, t in units.items() if not EXCLUDE.match(u)}


def main():
    agent = handle(os.environ.get("MOPROX_AGENT"))
    argv = sys.argv[1:]
    if not agent:
        print("regen: MOPROX_AGENT is not set, so I cannot tell whose posts these are", file=sys.stderr)
        return 2
    units = allowed(agent)
    if not argv or argv[0] == "list":
        if not units:
            print(f"no units have posted as #{agent}")
        for u, t in sorted(units.items()):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "(seen in its script)"
            desc = subprocess.run(["systemctl", "show", "-P", "Description", u],
                                  capture_output=True, text=True).stdout.strip()
            print(f"{u:40s} last posted {when}  {desc}")
        return 0
    if argv[0] == "run" and len(argv) == 2:
        u = argv[1] if argv[1].endswith(".service") else argv[1] + ".service"
        if u not in units:
            print(f"regen: {u} has never posted as #{agent}; run `regen list`", file=sys.stderr)
            return 1
        since = time.strftime("%Y-%m-%d %H:%M:%S")
        r = subprocess.run(["sudo", "-n", "systemctl", "start", u], capture_output=True, text=True,
                           timeout=900)
        log = subprocess.run(["journalctl", "-u", u, "--since", since, "-o", "cat", "--no-pager"],
                             capture_output=True, text=True).stdout.strip().splitlines()[-15:]
        print(f"{u}: {'finished' if r.returncode == 0 else 'FAILED (exit %d)' % r.returncode}")
        if r.stderr.strip():
            print(r.stderr.strip())
        print("\n".join(log))
        return r.returncode
    print(__doc__.split("\n\n")[-2], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
