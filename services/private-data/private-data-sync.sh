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
exec >>"$LOG" 2>&1
say() { echo "$(date -Is) $*"; }

cd "$REPO" || { say "FATAL: no $REPO"; exit 1; }

# Serialise against the mail-lane committers and any dev session working in the same tree.
# Lock is per-REPO (basename), so a second instance pointed at a different repo — e.g. the semantic
# memory store — runs concurrently instead of queueing behind this one for no reason.
LOCK="${PRIVATE_DATA_LOCK:-$(basename "$REPO")-sync.lock}"
exec 9>"/run/lock/$LOCK" 2>/dev/null || exec 9>"/tmp/$LOCK"
flock -w 300 9 || { say "busy: another sync holds the lock, skipping"; exit 0; }

# Never touch a tree someone left mid-rebase/merge — that needs a human.
if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ] || [ -f .git/MERGE_HEAD ]; then
  say "SKIP: rebase/merge in progress"; exit 0
fi

[ -z "$(git status --porcelain)" ] && exit 0

# Refuse to stage anything absurdly large; commit the rest.
big=$(git status --porcelain | awk '{print $NF}' | while read -r f; do
        [ -f "$f" ] && [ "$(stat -c%s "$f")" -gt $((MAX_MB * 1024 * 1024)) ] && echo "$f"
      done)
if [ -n "$big" ]; then
  say "SKIP: file(s) over ${MAX_MB}MB, needs a human: $(echo "$big" | tr '\n' ' ')"; exit 1
fi

# Summarise by top-level lane so the message says something ("sync: agents, polar (4 files)").
n=$(git status --porcelain | wc -l)
lanes=$(git status --porcelain | awk '{print $NF}' | cut -d/ -f1 | sort -u | paste -sd, | sed 's/,/, /g')

git add -A || { say "FATAL: git add failed"; exit 1; }
git commit -q -m "sync: $lanes ($n file(s))" \
  -m "Swept by private-data-sync.timer — producers that write here without committing." \
  || { say "FATAL: commit failed"; exit 1; }

# Another box (or the mail lane) may have pushed since; rebase our sweep on top rather than fail.
git pull -q --rebase origin main || { say "WARN: rebase failed, leaving commit local"; exit 1; }
git push -q origin main || { say "WARN: push failed, commit is local"; exit 1; }
say "pushed: $lanes ($n file(s))"
