#!/usr/bin/env python3
"""Retire `ai-feature-*` branches in theme-ontology/theming once their pull request is gone.

THE RULE (operator, 2026-09-04): **no `ai-feature-*` branch should exist without an OPEN PR.**
Those branches are machine-owned — M4 raises them, a human decides on them — so once the decision
has been made the branch has no reason to be there, and leaving it costs real work: the nightly
branch-sync merges master into every live branch, so five dead branches were being kept current
for months, and one of them (`ai-feature-sync-oscars`, PR merged 2026-09-01) was still generating
conflicts and waking M4 for a merge nobody would ever land.

WHAT HAPPENS TO A BRANCH, by what became of its PR:

  open PR              keep it. This is the only state in which the branch is doing anything.
  PR MERGED            the feature was taken; the content is on the base branch. Delete at once.
  PR CLOSED unmerged   a human looked and said no. Say so ONCE, leave the branch a week (GRACE_DAYS)
                       in case they want to look again, then delete it and say it was abandoned.
  no PR, ever          same clock as a rejection, but only after a SECOND consecutive sighting.
                       The watcher creates `ai-feature-sync-*` and opens its PR in separate API
                       calls, so a branch can legitimately have no PR for a few seconds; requiring
                       two daily sightings makes that race impossible to lose instead of guessing
                       at an age cutoff.

WHY A GRACE PERIOD ONLY FOR THE UNMERGED ONES. A merged PR's content is safe on master (or on
whatever it targeted) — the branch is a duplicate ref and deleting it loses nothing. Unmerged work
only exists on the branch, so its deletion is the one that is worth waiting on and worth announcing
twice: once when the clock starts, once when it runs out, with the tip sha so `git fetch
origin <sha>` still recovers it for as long as GitHub keeps the object.

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
GRACE_DAYS = int(os.environ.get("THEMING_BRANCH_GRACE_DAYS", "7"))
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

        # rejected / no-pr: both live on the same clock.
        seen_now.add(branch)
        if not was:
            entry = {"since": today().isoformat(), "why": state_, "sightings": 1,
                     "announced": False}
            if pr:
                entry["pr"] = pr["number"]
            pending[branch] = entry
            was = entry
        else:
            was["sightings"] = int(was.get("sightings") or 1) + 1
            # A branch that has never had a PR is only believed on the second consecutive sighting,
            # so the seconds between "create the sync branch" and "open its PR" cannot sweep it.
            if was.get("why") != state_:
                was["why"] = state_

        ready = was.get("why") != "no-pr" or int(was.get("sightings") or 1) >= 2
        if not ready:
            continue

        if not was.get("announced"):
            was["announced"] = True
            why = ("%s closed unmerged" % pr_link(pr)) if pr else "no PR was ever opened"
            notes.append("`%s` — %s; deleting %s unless it gets one"
                         % (branch, why, due(was).isoformat()))
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
