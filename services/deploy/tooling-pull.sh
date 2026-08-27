#!/usr/bin/env bash
# Keep the PRODUCTION checkout of moprox-tooling at origin/main.
#
# WHY THIS EXISTS: for months the 18 systemd units executed code straight out of
# ~/projects/moprox-tooling — the same working tree three interactive sessions and five agents
# develop in. Checking out a feature branch there silently changed what production ran, and a commit
# made while the tree sat on someone else's branch went nowhere useful. Deployment and development
# cannot share a working tree; webscout already solved this with /opt/webscout/repo + deploy.sh.
#
# This clone is NOT a workspace: it is reset --hard to origin/main every run. Never edit it, never
# commit in it — anything local is discarded without warning, which is the point.
#
# AND NEVER RUN git HERE AS ROOT. `sudo git pull` in this tree leaves the object fanout dirs it
# happens to create owned by root; this unit runs as mikael, so the NEXT fetch that needs one of
# those dirs — out of 256, so typically hours or DAYS later — dies with "insufficient permission for
# adding an object". Measured THREE times, every one of them by moprox-dev@one: 2026-08-08T19:32 ->
# 13 failures from 19:55; 2026-08-11T10:33 -> 57 failures from 08-14T05:15, i.e. 66.7 h after the
# cause; 2026-08-17T12:34 -> 32 failures from 15:25 and still failing when counted, 2.8 h after the
# cause. The lag is not a constant — 2.8 h vs 66.7 h — because the fetch only dies once a NEW object
# hashes into one of the (up to 256) fanout dirs the root pull happened to create. That variability
# is what makes it recur: whoever types it gets no feedback, and the rule below was already in this
# file, deployed and live for 2.7 days, when it was broken the third time. Prose did not work; the
# check below is the same knowledge in a form that runs.
#
# To deploy right now, do not reach for git at all:  sudo systemctl start tooling-pull.service
# (the unit runs as mikael and is the only supported writer of this tree).
set -Eeuo pipefail
PROD="${PROD:-/opt/moprox-tooling}"
BRANCH="${BRANCH:-main}"
LOG="${LOG:-/home/mikael/.local/state/tooling-pull.log}"
mkdir -p "$(dirname "$LOG")"
# Tee rather than redirect. `exec >>"$LOG" 2>&1` sent EVERYTHING to a file, so a failing unit showed
# up in journald and the incident queue with no reason at all — 13 consecutive failures whose cause
# ("insufficient permission for adding an object", from a root-owned .git) was only in a file nobody
# was watching. Repo rule: an unexpected error reaches the journal.
exec > >(tee -a "$LOG") 2> >(tee -a "$LOG" >&2)
say() { echo "$(date -Is) $*"; }
# <3> is the syslog level prefix journald turns into PRIORITY=3, so failures are findable with
# search_logs(priority=3) across the fleet.
die() { echo "<3>$(date -Is) $*" >&2; exit 1; }

cd "$PROD" || die "FATAL: $PROD missing"

# The root-pull fault above was, for three occurrences, detectable only by its consequence — and the
# consequence lags the cause by hours or days (67 h on 2026-08-11), which is precisely why the same
# session repeated it. It is detectable by its CAUSE in one cheap call that needs no privilege: this
# unit runs as mikael, so anything under .git it does not own was written by someone else, and the
# only writer that ever has been is `sudo git`. Report it before the fetch that will eventually trip
# over it, at err level, with the remedy — a latent fault nobody can see is worth less than a noisy
# one. Deliberately not fatal: most fetches still succeed while poisoned (10 deploys ran between the
# 2026-08-11 pull and the wave it caused, 66.7 h later), and refusing to deploy would turn a latent
# fault into an immediate outage.
# The `|| true` is load-bearing under `set -o pipefail`: find exits 1 on any unreadable directory,
# and an unreadable directory here IS the fault we are looking for (root-owned, mode 700), so the
# naive form would abort the deploy in exactly the case it exists to report.
alien=$( { find .git -not -user "$(id -un)" -printf . 2>/dev/null || true; } | wc -c )
if [ "$alien" -gt 0 ]; then
  echo "<3>$(date -Is) POISONED: $alien path(s) under $PROD/.git are not owned by $(id -un) — a" \
       "root git ran here. The next fetch needing one of them will die with 'insufficient permission" \
       "for adding an object'. Repairing now; the rule is: never run git as root in $PROD." >&2
  # ...and repair it, rather than printing the remedy for a human to run. Detection alone still
  # requires someone to be reading priority-3 logs between the poisoning and the wave, and the whole
  # reason this fault recurs is that nobody is: waves 1-3 each ran 23 min to 67 h before anyone
  # noticed. The chown is idempotent, needs no new sudoers entry (mikael has NOPASSWD:ALL today), and
  # is safe precisely because this unit is the only supported writer of the tree — nothing else here
  # is meant to be owned by anyone else. Report at err either way: a rule broken silently and fixed
  # silently is a rule nobody learns, and the point of the noise is to reach whoever broke it.
  if sudo -n chown -R "$(id -un):$(id -gn)" "$PROD" 2>/dev/null; then
    echo "<3>$(date -Is) repaired $alien path(s) — deploys unblocked, but fix the habit:" \
         "the timer pulls every 5 min, and 'sudo systemctl start tooling-pull.service' deploys NOW" >&2
  else
    echo "<3>$(date -Is) repair FAILED — deploys will freeze on the next fetch that needs one of" \
         "those objects. Run: sudo chown -R $(id -un):$(id -gn) $PROD" >&2
  fi
fi

before=$(git rev-parse --short HEAD)
# Retry, bounded, before calling it a failure. Once the self-repair above (b38287b) closed the
# root-pull class, the ENTIRE residual failure surface of this unit was transient network: measured
# 2026-08-18T14:00Z..2026-08-27T14:00Z on claude-dev's journal, 2596 clean runs and exactly 2
# failures, "Connection closed by 20.26.156.215 port 22" (08-21T14:05:52Z) and "Connection to
# github.com closed by remote host" (08-25T10:25:27Z), 0 poisonings. Both self-healed on the very
# next timer run 5 min later. Each nonetheless exited 1, which is a priority-3 line plus a
# `unit-failed` incident that heads the analyst's work queue for days — a queue with no recovery
# state, so the 2596 clean runs around them are invisible to it.
#
# Retry UNCONDITIONALLY rather than only on network-looking errors. This file already learned that
# lesson the expensive way (f01d015, and the comment below): matching the cause by string is
# confident and wrong the first time an unanticipated one appears. A blanket retry costs a poisoned
# tree ~20 s of extra stalling per run and still fails loudly with the real error; it costs a blip
# nothing at all. Report the attempt count so a retried success is not silently indistinguishable
# from a clean one, and keep the whole budget far inside the 5-minute timer period.
#
# Report what git ACTUALLY said, at err level. This line used to name one guessed cause on every
# failure ("a root-owned object blocks the mikael-run unit"). On 2026-08-13T14:35Z the real cause was
# `git@github.com: Permission denied (publickey)` — present in the journal, but only at priority 6,
# below the level the comment above sends triage to. The single priority-3 line said "check
# ownership". A hard-coded diagnosis on a generic failure is worse than no diagnosis: it is confident
# and wrong. Capture stderr so the cause and the alert are the same line.
fetch_ok=""
for attempt in 1 2 3; do
  # timeout: a hung TCP would otherwise let three attempts run unbounded into the next timer firing.
  # Fetches of this repo complete in ~1 s (08-21T14:10:58 -> :59), so 90 s is slack, not a limit.
  if fetch_err=$(timeout 90 git fetch -q origin "$BRANCH" 2>&1); then
    [ "$attempt" -gt 1 ] && say "fetch succeeded on attempt $attempt/3"
    fetch_ok=1
    break
  fi
  [ "$attempt" -lt 3 ] && sleep $(( attempt * 10 ))
done
if [ -z "$fetch_ok" ]; then
  die "fetch failed after 3 attempts: $(printf '%s' "$fetch_err" | tr '\n' ' ' | cut -c1-300)"
fi
after=$(git rev-parse --short "origin/$BRANCH")
[ "$before" = "$after" ] && exit 0

# Local state in a deployment tree is always a mistake; report it, then discard.
if [ -n "$(git status --porcelain)" ]; then
  say "WARN: local changes in the PRODUCTION tree — discarding: $(git status --porcelain | head -3 | tr '\n' ' ')"
fi
git reset -q --hard "origin/$BRANCH"
git clean -qfd
say "deployed $before -> $after ($(git log -1 --format=%s | cut -c1-70))"

# A unit whose code changed keeps running the old module until it restarts (the stale-daemon trap
# that once cost a false 'valet memory is blocked' report). Restart the long-lived importers only.
if git diff --name-only "$before" "$after" | grep -qE '^services/(agents|forward)/'; then
  # sudo -n: the unit runs as mikael; /etc/sudoers.d/tooling-pull grants exactly this one command.
  sudo -n systemctl restart dispatcher.service telegram-poll.service discord-theming.service 2>/dev/null \
    && say "restarted agent daemons (services/agents or services/forward changed)"
fi
