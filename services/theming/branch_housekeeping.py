#!/usr/bin/env python3
"""Retire `ai-feature-*` branches in theme-ontology/theming once their pull request is gone.

Those branches are machine-owned: M4 raises them, a human decides on them. Once the decision has
been made the branch has no reason to be there, and leaving it costs real work — the nightly
branch-sync merges master into every live branch, so five dead branches were being kept current for
months, and one of them (`ai-feature-sync-oscars`, PR merged 2026-09-01) was still generating
conflicts and waking M4 for a merge nobody would ever land.

THIS IS A GARBAGE COLLECTOR, NOT A RULE. "No ai-feature branch without an open PR" was the first
draft of this file and the operator corrected it (2026-09-04): a branch may perfectly well exist
before a PR, or all through an ongoing conversation, and the desired state is implied by the process
anyway. Nothing here should be read as a norm for M4 to comply with — an agent told that a branch
must have a PR will open one early to be obedient, which is worse than the mess it prevents. What is
actually worth detecting is a conversation that was ABANDONED, and a dead conversation has a proxy
that needs no rule: the branch stopped moving.

WHAT HAPPENS TO A BRANCH:

  open PR              keep it, whatever else is true.
  PR MERGED            the feature was taken; the content is on the base branch. Delete at once.
  PR CLOSED unmerged   a human looked and said no. Say so ONCE, leave the branch GRACE_DAYS in case
                       they want to look again, then delete it and say it was abandoned.
  no PR, still moving  LEAVE IT ALONE, silently. This is what work in progress looks like.
  no PR, gone quiet    no commit for STALE_DAYS. Retire it in the daily post, no warning phase: if
                       the conversation died before anyone opened a PR, the feature was probably not
                       wanted, so this is low-stakes tidying rather than a decision.

The quiet-branch clock is measured from the TIP COMMIT, not from when this sweep first noticed the
branch. That distinction is the whole correction: a sighting counter measures our own process and
would have swept a branch someone was still working on, where a commit date resets itself every time
the work continues and needs no state at all.

WHY A GRACE PERIOD ONLY FOR THE REJECTED ONES. A merged PR's content is safe on master (or on
whatever it targeted) — the branch is a duplicate ref and deleting it loses nothing, so there is
nothing to wait for. A closed-unmerged PR is the one case where a person made an explicit decision
against work that exists nowhere else, so it is the one worth pausing over and announcing twice:
once when the clock starts, once when it runs out. Both kinds of unmerged deletion carry the tip sha,
so `git fetch origin <sha>` recovers the branch for as long as GitHub keeps the object.

WHAT IT POSTS. One Discord message per run, and only when something CHANGED. A daily "nothing to
retire" is the noise this channel is not for; the estate already learned that from the valet's
repeated bullets and from branch_sync_watch's "all branches clear". Deletions and clock-starts are
changes; a branch quietly serving its open PR is not.

Auth: `gh` on claude-dev is odinlake-ai with team write on theme-ontology/theming — enough to list
and to delete a ref. Never touches a protected branch or anything not named `ai-feature-`.
"""
import datetime, json, os, sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "lib"))
sys.path.insert(0, str(HERE.parents[1] / "forward"))
sys.path.insert(0, str(HERE.parent))
import errlog                      # noqa: E402  — no silent swallows; see services/lib/errlog.py
import discord_api                 # noqa: E402  — the estate's one Discord transport
import branch_sync_watch as bsw    # noqa: E402  — REPO / gh_raw / protected(): one definition, not two

PREFIX = "ai-feature-"
# How long a branch survives a human's explicit "no" (a closed, unmerged PR), so they can change
# their mind or fish something out of it.
GRACE_DAYS = int(os.environ.get("THEMING_BRANCH_GRACE_DAYS", "7"))
# How long a branch with no PR may sit unchanged before it counts as an abandoned conversation.
# Deliberately longer than GRACE_DAYS and deliberately not a week: a week is the courtesy owed to a
# decision somebody made, and this is a staleness threshold on a conversation nobody ended. M4's
# conversations run in hours, so a fortnight of silence is unambiguous without needing to be right
# about the boundary.
STALE_DAYS = int(os.environ.get("THEMING_BRANCH_STALE_DAYS", "14"))
STATE = Path.home() / ".local/share/moprox/theming-branch-housekeeping.json"
# Report what WOULD happen and delete nothing. For a first run against a repo, or after changing
# the rules — a branch deleted by a bug is recoverable only while GitHub still has the object.
DRY = os.environ.get("THEMING_HOUSEKEEPING_DRY", "0") != "0"


def today():
    return datetime.date.today()


def gh_json(args, what):
    rc, out, err = bsw.gh_raw(args)
    if rc != 0:
        errlog.err("branch_housekeeping: %s: %s" % (what, err[:200]))
        return None
    try:
        return json.loads(out or "[]")
    except ValueError as e:
        errlog.err("branch_housekeeping: unreadable %s" % what, e)
        return None


def ai_branches():
    """[(name, tip sha)] for every `ai-feature-*` branch on the remote, or None if the call failed.

    Read as TEXT, not JSON: `--paginate` with `--jq` emits one line per row across all pages rather
    than one JSON document, so json.loads would choke on the second page.
    """
    rc, out, err = bsw.gh_raw(["api", "--paginate", "repos/%s/branches?per_page=100" % bsw.REPO,
                               "--jq", ".[] | [.name, .commit.sha] | @tsv"])
    if rc != 0:
        errlog.err("branch_housekeeping: listing branches: %s" % err[:200])
        return None
    pairs = []
    for ln in (out or "").splitlines():
        parts = ln.split("\t")
        if len(parts) == 2 and parts[0].startswith(PREFIX):
            pairs.append((parts[0], parts[1]))
    return sorted(pairs)


def prs_for(branch):
    """Every PR ever raised from this branch, newest first. None means the lookup failed."""
    prs = gh_json(["pr", "list", "--repo", bsw.REPO, "--head", branch, "--state", "all",
                   "--limit", "20", "--json", "number,state,mergedAt,closedAt,url,baseRefName"],
                  "listing PRs for %s" % branch)
    if prs is None:
        return None
    return sorted(prs, key=lambda p: p.get("number") or 0, reverse=True)


def verdict(prs):
    """(state, pr) — what the humans decided about this branch.

    An OPEN pr anywhere in the list wins outright: `ai-feature-sync-dev-themes` carries four merged
    PRs and one open one, because it is reused for every sync, and the open one is the live fact.
    """
    for p in prs:
        if p.get("state") == "OPEN":
            return "open", p
    if not prs:
        return "no-pr", None
    top = prs[0]
    return ("taken" if top.get("mergedAt") else "rejected"), top


def delete(branch):
    """Delete the remote ref. Refuses anything that is not an unprotected `ai-feature-*` branch."""
    if not branch.startswith(PREFIX) or bsw.protected(branch):
        errlog.err("branch_housekeeping: refusing to delete '%s' — not an unprotected %s branch"
                   % (branch, PREFIX))
        return False
    if DRY:
        print("DRY: would delete %s" % branch)
        return True
    rc, _, err = bsw.gh_raw(["api", "--method", "DELETE",
                             "repos/%s/git/refs/heads/%s" % (bsw.REPO, branch)])
    if rc != 0:
        errlog.err("branch_housekeeping: deleting %s failed: %s" % (branch, err[:200]))
        return False
    return True


def pr_link(pr):
    return discord_api.link("PR #%s" % pr["number"], pr["url"])


def last_commit(sha):
    """The date of a branch's tip commit, or None if it cannot be read.

    This is the abandonment signal, and it is deliberately a property of the BRANCH rather than of
    this sweep's bookkeeping: it resets itself the moment anyone commits again, so a conversation
    that is still going can never age out however long it runs.
    """
    rc, out, err = bsw.gh_raw(["api", "repos/%s/commits/%s" % (bsw.REPO, sha),
                               "--jq", ".commit.committer.date"])
    if rc != 0 or not out:
        errlog.warn("branch_housekeeping: cannot date commit %s: %s" % (sha[:10], err[:200]))
        return None
    try:
        return datetime.date.fromisoformat(out.strip()[:10])
    except ValueError as e:
        errlog.warn("branch_housekeeping: unreadable commit date %r for %s (%s)"
                    % (out[:40], sha[:10], e))
        return None


def due(entry):
    """The date a pending branch's grace period runs out."""
    return datetime.date.fromisoformat(entry["since"]) + datetime.timedelta(days=GRACE_DAYS)


def main():
    branches = ai_branches()
    if branches is None:
        return 1                    # the listing failed; do nothing rather than half a sweep
    try:
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
    except Exception as e:
        errlog.skip("branch_housekeeping: reading state", e)
        state = {}
    pending = state.get("pending") or {}
    notes, seen_now = [], set()

    for branch, sha in branches:
        prs = prs_for(branch)
        if prs is None:
            # One branch's lookup failing must not retire it by default. Keep any clock it already
            # has, skip it, and let the next run decide with real information.
            seen_now.add(branch)
            continue
        state_, pr = verdict(prs)
        was = pending.get(branch)

        if state_ == "open":
            if was:
                # A closed PR was reopened, or a new one raised from the same branch. Cancel the
                # clock silently: nothing was said when it started that needs taking back unless it
                # had been announced, and even then "never mind" is not worth a message.
                pending.pop(branch, None)
            continue

        if state_ == "taken":
            if delete(branch):
                notes.append("`%s` deleted — %s merged" % (branch, pr_link(pr)))
                pending.pop(branch, None)
            else:
                seen_now.add(branch)
            continue

        if state_ == "no-pr":
            # NOT a fault, and it gets no clock of its own and no warning. A branch with no PR is
            # either work in progress — which is none of this sweep's business — or a conversation
            # that stopped, and the tip commit is what tells them apart. Say nothing until it is
            # unambiguous, and then just retire it: nobody ended this one on purpose, so there is no
            # decision to defer to and no announcement anybody is waiting on.
            pending.pop(branch, None)
            when = last_commit(sha)
            if when is None:
                seen_now.add(branch)                # unreadable date: leave it entirely alone
                continue
            quiet = (today() - when).days
            if quiet < STALE_DAYS:
                continue
            if delete(branch):
                notes.append("`%s` deleted — no PR, quiet since %s; recover with "
                             "`git fetch origin %s`" % (branch, when.isoformat(), sha[:10]))
            else:
                seen_now.add(branch)
            continue

        # rejected: a human said no, so this one waits out GRACE_DAYS and is announced at both ends.
        seen_now.add(branch)
        if not was:
            was = pending[branch] = {"since": today().isoformat(), "announced": False,
                                     "pr": pr["number"]}

        if not was.get("announced"):
            was["announced"] = True
            notes.append("`%s` — %s closed unmerged; deleting %s unless it is reopened"
                         % (branch, pr_link(pr), due(was).isoformat()))
            continue

        if today() >= due(was):
            if delete(branch):
                notes.append("`%s` deleted — abandoned since %s, recover with `git fetch origin %s`"
                             % (branch, was["since"], sha[:10]))
                pending.pop(branch, None)
                seen_now.discard(branch)

    # A branch that vanished (merged and auto-deleted, or removed by hand) takes its clock with it.
    for gone in set(pending) - seen_now:
        pending.pop(gone, None)

    if notes:
        text = discord_api.items(notes)
        if len(notes) > 1:
            text = "**ai-feature branches**\n" + text
        try:
            discord_api.post(text)
            print("posted:", "; ".join(notes))
        except Exception as e:
            errlog.err("branch_housekeeping: posting to discord", e)
            return 1        # do NOT save state: retry next run rather than lose the announcement
    else:
        print("no change: %d ai-feature branch(es), %d on the clock"
              % (len(branches), len(pending)))

    state["pending"] = pending
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
