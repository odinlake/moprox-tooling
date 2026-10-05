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
# ONE exception, added deliberately and explained at the PRODUCERS table below: a derived artifact
# whose producer has no schedule anywhere is regenerated here before the sweep, because this is the
# only hourly unit in the repo that already runs on the box holding the tree. That step is
# make-style and non-fatal, so this file is still a backstop first.
#
# Install: cp services/private-data/private-data-sync.{service,timer} /etc/systemd/system/
set -uo pipefail

# Overridable so the script can be exercised against a throwaway clone before it is trusted here.
REPO=${PRIVATE_DATA_REPO:-/home/mikael/projects/private-data}
LOG=${PRIVATE_DATA_LOG:-/home/mikael/.local/state/private-data-sync.log}
MAX_MB=50   # GitHub hard-rejects >100 MB; bail loudly well before that rather than wedge the push

# Declared up here, not at the regen table below, because the rebase guard needs it: a conflict
# confined to these paths is a machine artifact this script made and can finish itself. The table's
# full rationale, and the format, are at the PRODUCERS use site further down.
# out|producer|input paths, all repo-relative.
PRODUCERS="finance/statements.json|finance/build.py|statements mail"
regen_outs() { printf '%s\n' "$PRODUCERS" | cut -d'|' -f1; }

# True when EVERY currently-conflicted path is one of our regen outputs, and there is at least one.
# `-x -F` on purpose: a substring or regex match here would let a conflict in, say,
# finance/statements.json.bak be treated as derived.
conflicts_are_derived() {
  local u outs p
  u=$(git diff --name-only --diff-filter=U 2>/dev/null) || return 1
  [ -n "$u" ] || return 1
  outs=$(regen_outs)
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    printf '%s\n' "$outs" | grep -qxF -- "$p" || return 1
  done <<<"$u"
  return 0
}

# A rebase nobody is sitting in front of: in progress, conflicts only in derived artifacts, and not
# touched for 30 min. A human resolving a conflict by hand touches .git/rebase-merge constantly —
# every `git add`, every `git rebase --continue` — so 30 min of silence is not a human at a keyboard.
# Deliberately NOT keyed on a marker file this script writes: the wedge this exists for was left by a
# build that wrote no marker, and a self-heal that cannot heal the wedge in front of it is theatre.
machine_rebase() {
  local d age
  for d in .git/rebase-merge .git/rebase-apply; do
    [ -d "$d" ] || continue
    age=$(( $(date +%s) - $(stat -c %Y "$d" 2>/dev/null || echo 0) ))
    [ "$age" -ge 1800 ] || { say "rebase dir $d touched ${age}s ago — assuming a human is in it"; return 1; }
  done
  conflicts_are_derived
}

# Complete a rebase whose conflicts are all derived artifacts, keeping OUR side at every step.
#
# --theirs, not --ours, and the distinction matters: mid-rebase `ours` is the commit being replayed
# ONTO (what the remote already has) and `theirs` is the local commit being replayed. We keep the
# LOCAL one because it is the newer regeneration AND because dropping it is not recoverable: regen()
# is make-style on mtimes, so once this box's out-file is replaced by the remote's content with a
# fresh mtime, nothing re-derives it until an INPUT changes again — the newer numbers would be gone
# with no error anywhere. Taking the local side costs at most one superseded regeneration on the
# remote, which the next run on either box re-derives.
#
# Empty-diff case is real and must not be an error: if the only change in the replayed commit was the
# derived file and the remote's content is identical after staging, `git rebase --continue` refuses
# with "nothing to commit"; --skip is the correct move and loses nothing.
finish_derived_rebase() {
  local p guard=0
  while [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; do
    guard=$((guard + 1))
    [ "$guard" -gt 20 ] && { err "WARN: derived-conflict resolution did not converge in $guard steps"; return 1; }
    conflicts_are_derived || return 1
    while IFS= read -r p; do
      [ -n "$p" ] || continue
      git checkout --theirs -- "$p" 2>/dev/null || return 1
      git add -- "$p" || return 1
    done < <(git diff --name-only --diff-filter=U)
    if git diff --cached --quiet; then
      git rebase --skip >/dev/null 2>&1 || true
    else
      GIT_EDITOR=true git rebase --continue >/dev/null 2>&1 || true
    fi
  done
  return 0
}

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

# Never touch a tree someone left mid-rebase/merge — that needs a human. Which is exactly why this
# is err + exit 1 and not `say` + exit 0, as it was: nothing here resolves the state, so this run
# publishes nothing and neither will the next one. A conflicted `git pull --rebase` — sync_push's own
# line 89, and memory-sync rebases on most runs — leaves precisely this state behind, so the script
# can wedge itself and then report the wedge as routine.
#
# Exit 0 hid it three ways at once, which is why the level alone was not enough: systemd saw a
# success, so no unit-failed — the ONLY incident kind this unit has ever been filed under, since
# logscan raises no log-errors for it at all (0 in 20 d, measured in
# moprox-memory/err-line-is-not-an-incident-private-data-sync); priority 6 is below what a priority
# query reads; and the watchdog's silence>4h rule is defeated by this very line arriving every hour.
# A repo that had stopped publishing indefinitely was indistinguishable from one with nothing to do.
# Same "needs a human" shape as the oversize guard below, now reported the same way.
#
# 2026-10-05 — the wedge the paragraph above predicted HAPPENED, and "needs a human" turned out to be
# both wrong and expensive. Measured: at 09:02:32Z this script regenerated finance/statements.json on
# claude-dev and swept it into b2e49fe; the push was rejected because claude-loop had pushed its own
# regeneration of the SAME derived file 54 min earlier (private-data dc33ef9); sync_push's
# `git pull --rebase` hit `CONFLICT (content): Merge conflict in finance/statements.json` and RETURNED
# WITHOUT ABORTING. From 09:15:57Z notif-ingest.service then failed every 15 min with
# `git commit ... returned non-zero exit status 128` — a wedge in one producer's unattended rebase
# takes down every OTHER writer of the shared tree, which is a blast radius the old comment did not
# consider, and nothing in the estate can reach the box to type `git rebase --abort`.
#
# So the guard now distinguishes the two cases instead of refusing both:
#  * a rebase whose every conflicted path is one of OUR regen outputs is machine-made — two boxes
#    independently re-derived the same artifact, and byte-difference is the regeneration stamp, not a
#    decision anyone made. finish_derived_rebase() completes it, keeping THIS box's version.
#  * anything else — a merge, a conflict in real data, a rebase a human is actively resolving — still
#    exits 1 untouched.
# The staleness test is what keeps a human's in-progress rebase safe: theirs was touched minutes ago,
# a wedge has been sitting for hours. No marker file is required for that path precisely because the
# wedge that motivated this was created by the version that had none.
if [ -f .git/MERGE_HEAD ]; then
  err "SKIP: merge in progress in $REPO, needs a human — this sweep published nothing and will keep publishing nothing until the tree is resolved"
  exit 1
fi
if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
  if machine_rebase && finish_derived_rebase; then
    say "finished a stale machine-made rebase (derived-artifact conflict only), keeping this box's regeneration"
  else
    err "SKIP: rebase in progress in $REPO, needs a human — this sweep published nothing and will keep publishing nothing until the tree is resolved"
    exit 1
  fi
fi

# Regenerate derived artifacts whose producer has no schedule of its own.
#
# WHY HERE: finance/statements.json is the ONE file the gated /finance dashboard is built from, and
# finance/amex-cycles.json is the estate's only Amex figure for cycles newer than the PDF archive
# (push alerts have carried no amount or merchant since 2026-08-06, issue i-20260814-154101, and
# statements/amex stops at 2026-06-10). Neither had a producer schedule of ANY kind. Measured
# 2026-10-04: the committed statements.json was generated 2026-09-04, 714 h / 29.75 d earlier, and
# in that window three landed producer fixes never reached the dashboard — it still served
# amex_cycles=110 with neither card_ceilings nor months_by_card, both of which build.py emits now.
# Freshness lanes exist for both artifacts and are the estate's two top incidents today, and both
# lane notes say the fix "needs a unit installed on a box this repo cannot reach". That was wrong,
# and written twice: a NEW unit does need a hand, because tooling-pull.sh deploys code and never
# installs unit files — but THIS script is already installed, already hourly, already runs as mikael
# on the box that holds the tree, and its ExecStart is redeployed from origin/main every 5 min. The
# schedule was reachable the whole time; only a new unit was not.
#
# Make-style, deliberately NOT unconditional. build.py stamps `generated` into its own output on
# every run and takes ~21 s to parse the PDFs, so running it hourly would commit a 100 kB file whose
# only delta is a timestamp, 24 times a day forever — churn in the repo that is the operator's
# backup. Regenerating only when an INPUT is newer holds the commit rate at the rate real finance
# data actually arrives, and the artifact's own stamp still advances whenever its evidence does.
#
# Non-fatal by construction. This script's job is to back the tree up, and that must not become
# hostage to a PDF parser: a producer that dies is reported at err — the repo rule is that an
# unexpected condition reaches the journal — and the sweep below still stages, commits and pushes
# everything else, including a PARTIAL regeneration, which is data a human needs to see.
#
# Keyed on the producer existing under $REPO, because memory-sync.service runs this very file
# against moprox-memory, which has no finance/ — so that invocation skips the table entirely, and so
# does a throwaway clone that does not carry the producer.
#
# out|producer|input paths, all repo-relative. The producer is invoked as `python3 <producer> <out>`;
# build.py takes its destination as argv[1] and transitively rewrites finance/amex-cycles.json, so
# this one row covers both stale lanes.
#
# ONE COPY, DECLARED AT THE TOP OF THE FILE, because the rebase guard runs before this point and needs
# the out column: a derived artifact regenerated independently on two boxes conflicts on content by
# construction, and that is the one conflict class this script may resolve on its own.

regen() {
  local out="$1" prod="$2" ins="$3" d stale="" ef rc=0 line so=""
  [ -f "$REPO/$prod" ] || return 0
  if [ ! -f "$REPO/$out" ]; then
    stale="absent"
  else
    for d in $ins; do
      [ -e "$REPO/$d" ] || continue
      # -print -quit: stop at the FIRST newer input rather than walking a 50k-file mail archive.
      if [ -n "$(find "$REPO/$d" -newer "$REPO/$out" -print -quit 2>/dev/null)" ]; then
        stale="$d newer"; break
      fi
    done
  fi
  [ -n "$stale" ] || return 0
  # The producer's stderr is kept SEPARATE from its stdout and re-emitted verbatim, one line at a
  # time, on BOTH the success and the failure path. This is services/update.py's run() idiom and it
  # is here for its reason: a captured child's stderr exists only in the capturing variable, so on
  # exit 0 it is dropped on the floor — which voids services/lib/errlog.py's contract, where a
  # `<3>`/`<4>` prefix on a child's stderr line is what becomes a real PRIORITY in the journal. An
  # aggregate warning about a PARTIAL result is precisely the thing a producer emits while still
  # exiting 0, and discarding it would make this step a place where an unexpected condition stops
  # reaching the journal. Line at a time, unmodified, so the prefixes stay at the start of the line
  # and journald still files each at the producer's own level rather than at this script's.
  #
  # stdout is deliberately NOT re-emitted on success: build.py writes a multi-line Amazon
  # reconciliation report there for a human at a terminal, it carries no level prefixes, and nothing
  # in the estate reads it. On a FAILURE its tail IS carried onto the err line, because a producer
  # that dies mid-report leaves the only account of how far it got there.
  #
  # What this makes visible, measured 2026-10-05 by running build.py for real: a SUCCESSFUL build
  # prints `Rotated text discovered. Output will be incomplete.` on stderr 18 times — pdftotext
  # saying it could not fully extract 18 of the statement PDFs this dashboard is built from. That is
  # unprefixed, so journald files it at the unit's own level and not at err, and it is the point:
  # the estate has never once seen that sentence.
  ef=$(mktemp "${TMPDIR:-/tmp}/regen-err.XXXXXX") || { err "WARN: mktemp failed, not regenerating $out"; return 0; }
  # timeout: build.py shells out to pdftotext per PDF, and a hung one would otherwise hold the lock
  # into the next firing. 21 s measured today, so 600 s is slack rather than a limit.
  so=$(timeout 600 python3 "$REPO/$prod" "$REPO/$out" 2>"$ef") || rc=$?
  while IFS= read -r line; do printf '%s\n' "$line" >&2; done <"$ef"
  if [ "$rc" -eq 0 ]; then
    say "regenerated $out ($stale)"
  else
    # Both buffers on the err line, stderr first: the producer's own diagnosis if it made one, and
    # the stdout tail if it died before it could. `finance/build.py` exits non-zero on a partial
    # Amex build (private-data 5328ef6), which is the shape this branch exists for.
    err "WARN: producer $prod exited $rc ($stale), $out may be stale or partial:" \
        "$({ cat "$ef"; printf '%s\n' "$so"; } | tr '\n' ' ' | tr -s ' ' | cut -c1-300)"
  fi
  rm -f "$ef"
}
printf '%s\n' "$PRODUCERS" | while IFS='|' read -r _o _p _i; do
  [ -n "$_o" ] && regen "$_o" "$_p" "$_i"
done

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
# unpaired: no producer takes it (moprox-memory/private-data-sync-lock-unpaired.md), so
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
    # "Leaving the commit local and retrying is the strictly safer failure: nothing is lost ... the
    # next run picks it up" — the paragraph above said that, and for a CONFLICT it was false in both
    # halves. The next run did not pick it up: it hit the rebase guard and exited 1 every hour, and
    # in between, notif-ingest.service crashed 8 times on `git commit` exit 128 because a tree
    # mid-rebase refuses commits from every producer, not just from us. Measured on claude-dev
    # 2026-10-05T09:02:34Z..11:02Z. So the rebase state does not survive this function any more:
    # resolve it if it is only a derived artifact, abort it otherwise. Both outcomes leave a tree
    # other producers can still commit to, which is the property that was actually missing.
    if finish_derived_rebase; then
      say "resolved a derived-artifact conflict during rebase, keeping this box's regeneration"
      if ! e=$(git push "$HAS_REMOTE" "$BRANCH" 2>&1); then
        err "WARN: push failed after derived-conflict rebase, commit is local: $(printf '%s' "$e" | tr '\n' ' ' | cut -c1-300)"
        return 1
      fi
      say "pushed after rebase: $what"; return 0
    fi
    # Safe HERE in a way it is not at the top-of-run guard: this rebase is seconds old and made by
    # this process, so the hard reset it performs can only discard what the rebase itself staged.
    git rebase --abort >/dev/null 2>&1 || git merge --abort >/dev/null 2>&1 || true
    err "WARN: push rejected and rebase failed, rebase ABORTED so other producers can still commit, leaving commit local: $(printf '%s' "$r" | tr '\n' ' ' | cut -c1-300)"
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
