#!/usr/bin/env bash
# work.sh <repo>: make (or refresh) this agent's scratch worktree and print its path.
#
# A change agent needs a tree that is (a) at origin/main, not at whatever the box's checkout
# happens to be, and (b) not the box's checkout, because the other loop agent commits there and a
# stray edit left in a shared tree rides along in someone else's commit. Doing that by hand is four
# git commands with two easy mistakes in them, and getting the base wrong costs a whole cycle: the
# harness lands on origin/main, so a patch cut from a stale base is rejected as stale.
#
# Idempotent: re-running throws the old worktree away and gives you a clean one at current
# origin/main. Anything you had not put in a proposal is gone, which is the intended trade: the
# proposal file is the durable artefact, the worktree is scratch.
set -euo pipefail

repo="${1:?usage: work.sh <moprox-tooling|private-data|moprox-memory>}"
agent="${LOOP_AGENT:-burndown}"
state="${LOOP_STATE:-$HOME/.local/share/moprox/loop/$agent}"
src="$HOME/projects/$repo"

[ -d "$src/.git" ] || { echo "no such repo: $src" >&2; exit 1; }

wt="$state/work/$repo"
mkdir -p "$(dirname "$wt")"

git -C "$src" fetch -q origin
# --force: the worktree may be gone from disk while git still lists it, which is what a killed
# cycle leaves behind. Prune first so `add` does not refuse a path it thinks is still registered.
# No -q: `git worktree prune` has no quiet flag (only -n/-v/--expire), and under `set -e` an
# unknown switch kills this script on its first line of real work.
git -C "$src" worktree remove --force "$wt" 2>/dev/null || true
rm -rf "$wt"
git -C "$src" worktree prune
git -C "$src" worktree add -q --detach "$wt" origin/main

echo "$wt"
