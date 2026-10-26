"""Who is acting? One answer for every audit record in the estate.

Every agent runs as the same Unix user with the same SSH key and the same git identity, so the
OS cannot tell them apart. What does differ is the systemd unit: moprox-dev@one, loop@burndown,
agent-memory-steward, telegram-poll. That is the identity, and it is read from the process's own
cgroup, so it is right from the first call without restarting anything.

Order: MOPROX_AGENT (set for every service by the 50-moprox-agent drop-in, and carried over SSH by
SendEnv), then the cgroup's unit, then AGENT_ID. An SSH session's cgroup is just session-N.scope,
which is why the env var wins.
"""
import os


def unit():
    try:
        for line in open("/proc/self/cgroup"):
            for part in reversed(line.strip().split(":")[-1].split("/")):
                if part.endswith(".service"):
                    return part[: -len(".service")]
    except OSError:
        pass
    return ""


def agent():
    a = os.environ.get("MOPROX_AGENT", "").strip()
    if a:
        return a
    u, aid = unit(), os.environ.get("AGENT_ID", "").strip()
    if u and aid and aid not in u:
        return f"{u}/{aid}"
    return u or aid or "unknown"
