# services/audit: who did what, on every box

Every agent in the estate runs as the same Unix user, with the same SSH key and the same GitHub
account. Until 2026-10-26 that made them indistinguishable after the fact: nobody could say which
agent raised claude-loop's RAM, and a dev session merged two PRs under a running loop cycle without
either side knowing. This directory makes every actor name itself, in the fleet log (mo.lan/logs),
where all of it can be searched together.

**Identity** (`who.py`) is the systemd unit: `moprox-dev@one`, `loop@burndown`, `telegram-poll`,
`agent-memory-steward`. It is read from the process's cgroup, so it is right without restarting
anything, and `MOPROX_AGENT` (set for every service by `/etc/systemd/system/service.d/50-moprox-agent.conf`)
carries it where the cgroup cannot, across SSH.

| Question | Record | Search it with (mo.lan/logs, or `search_logs`) |
|---|---|---|
| What did an agent do? | Claude Code PreToolUse hook, one line per non-read tool call, written BEFORE it runs | ident `agent-action` |
| Who ran which git command? | git trace2 for every git process on the box, state changes only | ident `git-trace` |
| Who logged in to this box over SSH? | `/etc/ssh/sshrc` logs the agent the client declared | ident `agent-ssh` |
| Who is working on what right now? | `estate-intent open/close/check`; the loop opens one per cycle | ident `estate-intent` |
| Who merged this PR? | the `gh` wrapper writes `Merged-by:` (and any concurrent intents) into the squash commit | `git log` |
| Who changed a guest on the Proxmox host? | the host's journal (pvedaemon logs the acting user) | ident `pve-host` |

**git identity.** Author is `odinlake-ai <agent@odinlake.net>` (global config) for everything on
claude-dev and claude-loop. The committer names the unit (`GIT_COMMITTER_NAME=odinlake-ai (%N)`
from the drop-in), so `git log --format='%an | %cn'` shows which agent made a commit. A service
picks the env var up on its next restart; until then its commits show committer `odinlake-ai`
and its git-trace lines still name it, via INVOCATION_ID.

**Before you merge or deploy:** `estate-intent check repo:<name>` (the gh wrapper does this for
`gh pr merge` and prints anything open). For long work on shared state, `estate-intent open
--ttl MIN SCOPE what` and `close ID` when done. Nothing is ever refused: this is visibility, not
locking.

**Not done: one SSH key per agent.** Every session, doc and memory fact says
`ssh -i ~/.ssh/claude-dev-ops`, so separate keys would only be as good as everyone's habits.
SendEnv-based tagging gets the same attribution with nothing to remember. It is self-declared,
which is fine: this is attribution among our own agents, not authentication.

## Install

    sudo services/audit/install-agent-box.sh            # claude-dev (prod tree) and claude-loop
    ssh agent@<box> 'sudo bash -s' < services/audit/ssh-tag.sh     # every box, including those two

On claude-dev also install `pve-journal-pull.{service,timer}`. It needs one grant on the PVE
host, run as root there (the token is read-only PVEAuditor, which lacks Sys.Syslog):

    pveum role add EstateLogReader -privs Sys.Syslog
    pveum acl modify /nodes/moprox -users metrics@pve -roles EstateLogReader
    pveum acl modify /nodes/moprox -tokens 'metrics@pve!collector' -roles EstateLogReader

Until then the puller logs one warning per run and ships nothing.

Tests: `python3 services/audit/tests/test_audit.py`, and `services/loop/tests/test_land.py`.
