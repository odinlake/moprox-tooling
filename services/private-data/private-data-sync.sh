#!/usr/bin/env bash
# Catch-all committer for private-data.
#
# WHY: private-data is written by many producers — polar-fetch (every 5 min), notif-ingest/amex
# (every 15 min), the coach/valet agents rewriting their own memory files, plus ad-hoc dev work.
# Only the mail lane (email-search/categorize-nightly.sh, backfill-msgid.py) ever committed its own
# subtree, so everything else silently accumulated as uncommitted work until a human noticed. This
# sweeps whatever is dirty on a schedule so the repo is always a faithful backup of the box.
#
# Producers that DO commit their own subtree with a meaningful message keep doing so — this only
# ever sees what they left behind, so their history stays well-labelled and this stays a backstop.
#
# Install: cp services/private-data/private-data-sync.{service,timer} /etc/systemd/system/
set -uo pipefail

# Overridable so the script can be exercised against a throwaway clone before it is trusted here.
REPO=${PRIVATE_DATA_REPO:-/home/mikael/projects/private-data}
LOG=${PRIVATE_DATA_LOG:-/home/mikael/.local/state/private-data-sync.log}
MAX_MB=50   # GitHub hard-rejects >100 MB; bail loudly well before that rather than wedge the push

mkdir -p "$(dirname "$LOG")"
# Tee rather than redirect. `exec >>"$LOG" 2>&1` sends EVERYTHING to a file, so a unit that exits
# non-zero reaches journald and the incident queue with NO reason attached — which is exactly what
# happened to this unit on 2026-08-21T14:04:03Z: incident_detail() joined on that invocation returns
# zero rows, and the only message is systemd's own "Failed to start ...". The rule this violated is
# stated verbatim in services/deploy/tooling-pull.sh, where the same bug cost 13 undiagnosable
# failures: an unexpected error reaches the journal. Written there 2026-08-09, unfixed here for 12
# days, because a rule in one file's comments does not run anywhere else.
exec > >(tee -a "$LOG") 2> >(tee -a "$LOG" >&2)
say() { echo "$(date -Is) $*"; }
# <3> is the syslog level prefix journald turns into PRIORITY=3, so failures are findable with
# search_logs(priority=3) and by the logscan incident kind, not just by tailing a file on the box.
err() { echo "<3>$(date -Is) $*" >&2; }

cd "$REPO" || { err "FATAL: no $REPO"; exit 1; }

# Serialise against the mail-lane committers and any dev session working in the same tree.
# Lock is per-REPO (basename), so a second instance pointed at a different repo — e.g. the semantic
# memory store — runs concurrently instead of queueing behind this one for no reason.
LOCK="${PRIVATE_DATA_LOCK:-$(basename "$REPO")-sync.lock}"
# The braces are load-bearing. `exec 9>"/run/lock/$LOCK" 2>/dev/null` was the previous form, and
# `exec` WITHOUT a command applies EVERY redirection on the line to the shell permanently — so on the
# common path where /run/lock is writable, that line silently pointed this script's stderr at
# /dev/null for the rest of the run. Every diagnostic below is on stderr. Scoping the 2>/dev/null to
# a group restores fd 2 when the group ends, while `exec` inside a group (not a subshell) still binds
# fd 9 in the current shell. Verified: with the old form, a later `echo x >&2` produced nothing.
if ! { exec 9>"/run/lock/$LOCK"; } 2>/dev/null; then exec 9>"/tmp/$LOCK"; fi
flock -w 300 9 || { say "busy: another sync holds the lock, skipping"; exit 0; }

# Never touch a tree someone left mid-rebase/merge — that needs a human.
if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ] || [ -f .git/MERGE_HEAD ]; then
  say "SKIP: rebase/merge in progress"; exit 0
fi

BRANCH="$(git symbolic-ref --quiet --short HEAD || echo main)"
HAS_REMOTE=$(git remote | head -1)

# A clean tree does NOT mean there is nothing to do: producers like the notifications ingest COMMIT
# without pushing, so commits can sit local indefinitely while the tree looks spotless (4 were sitting
# when this was found, 2026-08-03). Only exit early when the tree is clean AND nothing is unpushed.
unpushed() {
  [ -n "$HAS_REMOTE" ] || { echo 0; return; }
  git rev-list --count "@{u}..HEAD" 2>/dev/null || echo 0
}

# Push FIRST, rebase only if the push is REJECTED. Both call sites used to pull --rebase
# unconditionally and then push, and that ordering is what failed on claude-dev at
# 2026-09-09T05:00:51Z with "cannot pull with rebase: You have unstaged changes".
#
# The tree is routinely dirty again by the time we reach here, because this script's flock is
# unpaired: no producer takes it (moprox-memory/private-data-sync-lock-is-unpaired.md), so
# polar-fetch (every 5 min) and notif-ingest (every 15 min) keep writing straight through our own
# `git add -A && git commit`. A rebase needs a clean working tree; a push does not touch the
# working tree at all. Reproduced deterministically with a post-commit hook standing in for the
# racing producer: pull --rebase exits 1 on that tree, `git push` on the SAME tree succeeds.
#
# And the pull was ceremony in the observed failure — the remote had not moved. Only a genuinely
# diverged remote makes a rebase necessary, and git tells us that by rejecting the push.
#
# No --autostash on the rebase, deliberately. It would clear this same dirty-tree block, but a
# conflicting stash pop leaves conflict markers in the tree, which the NEXT sweep would commit as
# data. Leaving the commit local and retrying is the strictly safer failure: nothing is lost, the
# lines below say so at err, and the next run picks it up.
sync_push() {
  local what="$1" e r
  if e=$(git push "$HAS_REMOTE" "$BRANCH" 2>&1); then
    say "pushed: $what"; return 0
  fi
  if ! r=$(git pull --rebase "$HAS_REMOTE" "$BRANCH" 2>&1); then
    err "WARN: push rejected and rebase failed, leaving commit local: $(printf '%s' "$r" | tr '\n' ' ' | cut -c1-300)"
    return 1
  fi
  if ! e=$(git push "$HAS_REMOTE" "$BRANCH" 2>&1); then
    err "WARN: push failed after rebase, commit is local: $(printf '%s' "$e" | tr '\n' ' ' | cut -c1-300)"
    return 1
  fi
  say "pushed after rebase: $what"
}
if [ -z "$(git status --porcelain)" ]; then
  if [ "$(unpushed)" -eq 0 ] 2>/dev/null; then exit 0; fi
  say "clean tree but $(unpushed) unpushed commit(s) — pushing"
  # Deliberately still exit 0, as before: this path leaves the commits safely local and the next
  # run retries. sync_push has already said why at err.
  sync_push "$(unpushed) pending commit(s)"
  exit 0
fi

# -uall is load-bearing, not a nicety. Plain `git status --porcelain` COLLAPSES a directory whose
# contents are all untracked into ONE entry ("?? ultrahuman/"), so the size loop below tested
# `[ -f ultrahuman/ ]` — false — and skipped all 136 files inside it. That is precisely how a new
# lane arrives, which is the only way a huge file has ever reached this repo: 334 of the 384 files
# this sweeper has ever added (87%) were hidden behind a collapsed entry and never size-checked.
# The same collapse made the commit message lie — f0618f32 says "1 file(s)" for 136, a6db02c "5"
# for 195. One status call, used for all three, so the guard and the message can no longer disagree
# with what is about to be committed.
#
# Path extraction: cut the 3-byte "XY " prefix and any rename arrow rather than awk '{print $NF}',
# which splits a path containing a space and returns only its tail.
STATUS=$(git status --porcelain -uall)
paths() { printf '%s\n' "$STATUS" | sed -e 's/^...//' -e 's/.* -> //' -e 's/^"//' -e 's/"$//'; }

# Refuse to stage anything absurdly large; commit the rest.
big=$(paths | while read -r f; do
        [ -f "$f" ] && [ "$(stat -c%s "$f")" -gt $((MAX_MB * 1024 * 1024)) ] && echo "$f"
      done)
if [ -n "$big" ]; then
  err "SKIP: file(s) over ${MAX_MB}MB, needs a human: $(echo "$big" | tr '\n' ' ')"; exit 1
fi

# Summarise by top-level lane so the message says something ("sync: agents, polar (4 files)").
n=$(printf '%s\n' "$STATUS" | grep -c .)
lanes=$(paths | cut -d/ -f1 | sort -u | paste -sd, | sed 's/,/, /g')

git add -A || { err "FATAL: git add failed"; exit 1; }
git commit -q -m "sync: $lanes ($n file(s))" \
  -m "Swept by private-data-sync.timer — producers that write here without committing." \
  || { err "FATAL: commit failed"; exit 1; }

# A repo with no remote yet (the memory store, until its GitHub repo exists) is a normal state, not a
# failure: commit locally and stop cleanly, so systemd doesn't mark the unit failed every run.
if [ -z "$HAS_REMOTE" ]; then
  say "committed locally: $lanes ($n file(s)) — no remote configured yet"; exit 0
fi
# Another box (or the mail lane) may have pushed since; sync_push rebases our sweep on top rather
# than fail, but only once git has told us the remote actually moved. Report what git ACTUALLY
# said — dropping -q and letting git's own stderr flow through the tee is NOT enough: a service's
# stderr lands in the journal at priority 6, below the level triage reads, the same trap
# tooling-pull.sh hit on 2026-08-13 where the real cause (publickey denied) was in the journal all
# along and invisible. sync_push captures it and re-emits it on the err line itself.
sync_push "$lanes ($n file(s))" || exit 1
