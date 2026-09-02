#!/usr/bin/env python3
"""Read the theming repo's branch-sync status and tell #exocomps when a branch needs a human.

The workflow (theming .github/workflows/branch-sync.yml) rebases or merges master into every live
branch and writes ONE artifact describing all of them. It never fails on a conflict — a conflict is
a fact about the repo, not a broken job — so nothing in GitHub's UI goes red and nobody finds out.
This closes that loop: pull the newest status, and post when a branch is stuck.

WHAT IT POSTS, AND WHAT IT DOES NOT
  Only conflicts and push rejections, and only when the SET of them changes. The same two branches
  conflicting every night at 04:17 is one message, not a nightly drip — the estate already learned
  that lesson with the valet's repeated-bullet failure. A conflict clearing is worth saying once too,
  so the channel reflects the current state rather than a stream of alarms.

DISPATCHING M4. A conflict this script can only DESCRIBE is one nobody acts on: the report has been
posting the same branches for months. So a NEWLY-appearing conflict now wakes the theming agent,
once, with the diagnosis already in hand. The agent's remit is deliberately narrow (see PROMPT
below): structural resolutions only — ordering, formatting, a change already upstream under a
different SHA — never a decision about what the prose SAYS. That line exists because this repo is a
curated knowledgebase whose AGENTS.md is built around human review; rearranging fields is
housekeeping, choosing whose wording survives is editorial.

Auth: `gh` on claude-dev is odinlake-ai with team write on theme-ontology/theming, which is enough
to list runs and download artifacts. Discord goes through services/forward/discord_api.py — the same
bot and channel the theming agent uses, so a human can reply to it there.
"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "lib"))
sys.path.insert(0, str(HERE.parents[1] / "forward"))
import errlog                      # noqa: E402  — no silent swallows; see services/lib/errlog.py
import discord_api                 # noqa: E402  — the estate's one Discord transport

REPO = os.environ.get("THEMING_REPO", "theme-ontology/theming")
WORKFLOW = "branch-sync.yml"
STATE = Path.home() / ".local/share/moprox/branch-sync-seen.json"
STUCK = ("conflict", "push-rejected", "needs-pr", "error")
# Only real merge conflicts are worth an agent. A push rejection is a permissions or plumbing fact
# and there is nothing for it to resolve.
DISPATCH_ACTIONS = ("conflict",)
# Bound the spend. Three untouched conflicts in one night is already unusual; more than that is a
# repo-wide event (a reformat landing on master) where one agent per branch is the wrong response
# and a human should look once.
MAX_DISPATCH = int(os.environ.get("BRANCH_SYNC_MAX_DISPATCH", "3"))
DISPATCH = os.environ.get("BRANCH_SYNC_DISPATCH", "1") != "0"
# Protected branches take a PR; everything else the agent may push to directly (operator's call,
# 2026-09-01). Kept as a prefix list rather than an API probe so this file has no extra failure mode
# — if it is wrong it is wrong towards MORE review, and the push would be refused anyway.
PROTECTED_PREFIXES = ("master", "main", "dev-")
# Requested on every sync PR, and assigned. GitHub refuses to make the AUTHOR a reviewer, and the
# author here is odinlake-ai — a different account from odinlake — so both of these are eligible.
# The PRs that started this had NO reviewers, which is why the operator kept having to request a
# review from himself before he could approve one.
PR_AUTHOR = os.environ.get("BRANCH_SYNC_PR_AUTHOR", "odinlake-ai")
PR_REVIEWERS = [r for r in os.environ.get("BRANCH_SYNC_REVIEWERS",
                                          "odinlake,paul-sheridan").split(",") if r]


def gh(*args, check=True):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=180)
    if check and r.returncode != 0:
        raise RuntimeError("gh %s: %s" % (" ".join(args), (r.stderr or r.stdout).strip()[:300]))
    return r.stdout


def latest_status():
    """The status JSON from the most recent completed run, or None if there isn't one yet."""
    try:
        listing = gh("run", "list", "--repo", REPO, "--workflow", WORKFLOW,
                     "--status", "completed", "--limit", "1",
                     "--json", "databaseId,conclusion,createdAt,url")
    except RuntimeError as e:
        # The workflow not existing yet (PR not merged) is a state, not a fault. Anything else is.
        if "not found" in str(e).lower():
            return None, None
        raise
    runs = json.loads(listing)
    if not runs:
        return None, None
    run = runs[0]
    with tempfile.TemporaryDirectory() as td:
        gh("run", "download", str(run["databaseId"]), "--repo", REPO,
           "--name", "branch-sync-status", "--dir", td)
        files = list(Path(td).rglob("*.json"))
        if not files:
            raise RuntimeError("run %s has no status json in its artifact" % run["databaseId"])
        return json.loads(files[0].read_text()), run


def render(status, run, stuck, cleared):
    """The message a human reads at a glance.

    LEAD WITH ONE CONCRETE FACT, then the counts. The old version pasted every conflicting path and
    the first 300 characters of git's push error, which for a push rejection is four lines of
    generic `hint:` text that says the same thing for every branch and names nothing. That is a dump,
    not a report: it took a session of digging to learn that two of the branches had no conflict at
    all. So each line now says WHERE the first conflict is -- file and line -- and what the two
    sides actually put there, which is the thing a person needs before deciding anything.
    """
    lines = []
    for b in stuck:
        first = b.get("conflict_first") or {}
        files = b.get("conflict_files") or []
        head = "**%s** — %s (%s ahead, %s behind)" % (b["branch"], b["action"], b["ahead"], b["behind"])
        if b.get("action") == "needs-pr":
            # Not a fault: master and dev-* are protected on purpose, so the sync can compute the
            # merge but a human has to land it. Saying so stops it reading like a broken job.
            lines.append(head + "\n  ↳ protected branch — the merge is ready but only a "
                                "human-reviewed PR can land it")
            continue
        if first.get("file"):
            where = "%s:%s" % (first["file"], first.get("line") or "?")
            lines.append(head + "\n  ↳ first conflict at `%s` — %s" % (where, first.get("why", "")))
            for side, label in (("ours", "branch"), ("theirs", "master")):
                if first.get(side):
                    lines.append("      %-7s %s" % (label + ":", "; ".join(first[side])[:150]))
            if len(files) > 1:
                lines.append("      and %d more file(s): %s" % (len(files) - 1,
                                                                ", ".join(files[1:4])))
        elif files:
            lines.append(head + "\n  ↳ %d file(s): %s" % (len(files), ", ".join(files[:3])))
        else:
            # A push error's useful part is its FIRST line; the rest is git's generic hint block.
            detail = (b.get("error") or "").strip().splitlines()
            useful = next((ln for ln in detail if "hint:" not in ln and ln.strip()
                           and not ln.startswith("To ")), detail[0] if detail else "")
            lines.append(head + ("\n  ↳ " + useful[:180] if useful else ""))
    if cleared:
        lines.append("Cleared since last check: " + ", ".join(sorted(cleared)))
    auto = [b for b in status["branches"] if b.get("auto_resolved")]
    if auto:
        lines.append("Settled mechanically (equivalent content, not a disagreement): "
                     + "; ".join("%s %d hunk(s)" % (b["branch"], len(b["auto_resolved"]))
                                 for b in auto[:5]))
    counts = ", ".join("%s %s" % (v, k) for k, v in sorted(status["summary"].items()))
    lines.append("_%s · base %s · %s_"
                 % (counts, status.get("base_sha", "?"), discord_api.link("run", run["url"])))
    # This posts through the same bot account M4 replies from, so it looks like M4 spoke — and it
    # is not M4, it is a cron job with no ability to reason about what it just pasted. Say so, and
    # point at the thing that CAN: @M4 reads this channel and can open the repo.
    lines.append("_automated report — @M4 for a diagnosis of any line_")
    return "\n".join(lines)


def gh_raw(args):
    """(returncode, stdout, stderr) from `gh`. Status codes matter here — 409 is a real answer."""
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=180)
    return r.returncode, r.stdout.strip(), (r.stderr or "").strip()


def sync_pr(branch):
    """Keep a PROTECTED branch's sync PR open and current. Returns a one-line status, or None.

    Done entirely through the GitHub API — no clone, no local git, no agent. Merging master into a
    branch is mechanical: there is nothing here for a model to decide unless it conflicts, and
    /repos/../merges performs the merge server-side and answers 409 when it cannot. So the ordinary
    case costs one HTTP call and cannot go wrong in an interesting way; only a genuine conflict
    wakes M4.

    Idempotent by construction: it merges into the same ai-feature-sync-<branch> head every time, so
    a branch that drifts further just gets more commits on the PR that is already open, which is
    what "keep me updated while it is open" means. A second PR would be noise, not an update.
    """
    head = "ai-feature-sync-%s" % branch
    rc, sha, _ = gh_raw(["api", "repos/%s/git/ref/heads/%s" % (REPO, head), "--jq", ".object.sha"])
    if rc != 0:
        rc, base_sha, err = gh_raw(["api", "repos/%s/git/ref/heads/%s" % (REPO, branch),
                                    "--jq", ".object.sha"])
        if rc != 0:
            errlog.err("branch_sync_watch: cannot read %s to start its sync branch: %s"
                       % (branch, err[:200]))
            return None
        rc, _, err = gh_raw(["api", "repos/%s/git/refs" % REPO, "--method", "POST",
                             "-f", "ref=refs/heads/%s" % head, "-f", "sha=%s" % base_sha])
        if rc != 0:
            errlog.err("branch_sync_watch: cannot create %s: %s" % (head, err[:200]))
            return None

    rc, out, err = gh_raw(["api", "repos/%s/merges" % REPO, "--method", "POST",
                           "-f", "base=%s" % head, "-f", "head=master",
                           "-f", "commit_message=Merge master into %s (branch sync)" % branch])
    if rc != 0 and ("409" in err or "conflict" in err.lower()):
        return "CONFLICT"                       # the caller escalates this one to M4
    if rc != 0:
        errlog.err("branch_sync_watch: merging master into %s failed: %s" % (head, err[:200]))
        return None
    moved = bool(out and out != "null")         # 204 (empty) means already up to date

    rc, existing, _ = gh_raw(["pr", "list", "--repo", REPO, "--head", head, "--state", "open",
                              "--json", "number", "--jq", ".[0].number"])
    if existing:
        if not moved:
            return None
        return "%s drifted further — %s updated" % (
            branch, discord_api.link("PR #%s" % existing,
                                     "https://github.com/%s/pull/%s" % (REPO, existing)))
    if not moved:
        return None                             # nothing to propose and nothing open: silence
    rc, url, err = gh_raw(["pr", "create", "--repo", REPO, "--base", branch, "--head", head,
                           # Every autogenerated PR title starts with M4, so the PR list says at a
                           # glance which were opened by a machine (operator's rule, 2026-09-02;
                           # the two totolo workflows carry the same prefix via theming#719).
                           "--title", "M4: sync %s with master" % branch,
                           "--body", "Keeps `%s` current with master. Opened automatically because "
                                     "`%s` is a protected branch, so the nightly branch-sync job "
                                     "can compute this merge but cannot push it.\n\nMerge commits "
                                     "only — no content originates here." % (branch, branch),
                           "--assignee", ",".join(PR_REVIEWERS),
                           "--reviewer", ",".join(PR_REVIEWERS)])
    if rc != 0:
        errlog.err("branch_sync_watch: opening the sync PR for %s failed: %s" % (branch, err[:200]))
        return None
    u = (url.splitlines()[-1] if url else "").strip()
    num = u.rsplit("/", 1)[-1] if u else "?"
    return "%s behind master — %s" % (branch, discord_api.link("PR #" + num, u) if u else "PR opened")


def merge_approved_sync_prs():
    """Merge any open sync PR a human has APPROVED. Returns one line per merge.

    This is GitHub's auto-merge, done here instead, because doing it natively is not free: auto-
    merge only engages while something is still PENDING on the PR, and dev-themes/dev-stories
    require nothing — no approvals, no checks, no rulesets. Making auto-merge meaningful would mean
    adding "require 1 approving review" to dev-*, and that would force Paul and the operator to
    open a PR for the small direct commits they currently push straight to those branches. Changing
    how two people work, to save one click on a robot's housekeeping PR, is the wrong trade. (It is
    also not available: odinlake-ai is not an admin on the repo.)

    Scope is deliberately narrow: PRs opened by this automation, whose head is ai-feature-sync-*,
    and only once reviewDecision is APPROVED. AGENTS.md rule 3 says do not merge your own PR; the
    spirit of it is that a human decides, and here one has — this only carries out that decision,
    exactly as GitHub's own auto-merge would.
    """
    rc, out, err = gh_raw(["pr", "list", "--repo", REPO, "--state", "open", "--limit", "20",
                           "--json", "number,headRefName,baseRefName,reviewDecision,author"])
    if rc != 0:
        errlog.err("branch_sync_watch: cannot list open PRs to auto-merge: %s" % err[:200])
        return []
    try:
        prs = json.loads(out or "[]")
    except ValueError as e:
        errlog.err("branch_sync_watch: unreadable PR list", e)
        return []
    done = []
    for pr in prs:
        if not (pr.get("headRefName") or "").startswith("ai-feature-sync-"):
            continue
        if (pr.get("author") or {}).get("login") != PR_AUTHOR:
            continue
        if pr.get("reviewDecision") != "APPROVED":
            continue
        rc, _, err = gh_raw(["pr", "merge", str(pr["number"]), "--repo", REPO,
                             "--merge", "--delete-branch"])
        if rc != 0:
            errlog.err("branch_sync_watch: PR #%s is approved but would not merge: %s"
                       % (pr["number"], err[:200]))
            continue
        # Deliberately NOT returned as a Discord note. GitHub already tells the person who
        # approved it that their PR merged; repeating that here is a message saying something has
        # been dealt with, which is the definition of nothing to do.
        print("merged #%s — %s now in sync with master" % (pr["number"], pr.get("baseRefName")))
    return []


def protected(branch):
    return any(branch == pre.rstrip("-") or branch.startswith(pre) for pre in PROTECTED_PREFIXES)


def sig(b):
    """What makes a conflict THIS conflict. Re-dispatch when it changes, not when it recurs.

    Keyed on the conflicting paths rather than the commit shas: the same unresolved conflict drifts
    a sha every time master moves, and keying on that would wake the agent nightly for a branch it
    already declined to touch."""
    return "%s:%s" % (b["branch"], ",".join(sorted(b.get("conflict_files") or [])))


PROMPT = """A branch-sync conflict in theme-ontology/theming needs you. The nightly job could not
merge master into `%(branch)s`, and the mechanical resolver in .github/scripts/branch_sync.py
already declined it — so this is NOT ordering or whitespace, it is a real textual conflict.

  branch:   %(branch)s (%(ahead)s ahead, %(behind)s behind master)
  files:    %(files)s
  first:    %(first)s
  run:      %(url)s

YOUR REMIT IS STRUCTURAL, AND THE LINE IS NOT NEGOTIABLE. Resolve it ONLY if the conflict is about
form rather than meaning. That includes: field or block ORDERING; formatting and whitespace; a
duplicate of a change that is ALREADY on master under a different sha (the usual cause here — a
branch's work reached master through a squashed or rebased PR, so git can no longer see the two as
the same change); and one side being the other plus more.

If resolving would change what the prose SAYS — an annotation motivation, a theme description, a
title or date — STOP. Do not choose between two people's wording, do not merge them into a third
version, do not "improve" either. Report it instead. This repo is a curated knowledgebase and its
AGENTS.md is built around human review; that is the whole reason you have a narrow remit here.

If you do resolve it:
  1. Work in ~/projects/theming. Fetch first, then
     `git switch -c ai-feature-sync-%(branch)s origin/%(branch)s` — that exact name, from the
     start, because it is the name it has to be pushed under and renaming later wastes a step.
     Never put the word "master" in a branch name. Then `git merge origin/master`, fix the
     conflicted hunks, and check with `git diff origin/master -- <file>` — you should be able to
     say exactly what survived and why.
  2. VALIDATE before anything leaves the machine:
       PYTHONPATH=~/projects/python-totolo python3 -c \
         "import totolo; o=totolo.files('./notes'); assert len(o.story)>0 and len(o.theme)>0"
     If that fails, or you cannot run it, do NOT push — open a PR and say it is unvalidated.
  3. LANDING IT: %(sensitivity)s
  4. Never bypass the pre-push hook (it says so itself), never force-push, never merge your own PR.
     The hook allows a sync merge onto an unprotected branch and refuses new CONTENT there, which
     is the line to stay on: you are syncing, not authoring. If a push is refused, say so and leave
     the branch ready — do not invent a way around it. That is a correct outcome, not a failure.

POSTING. ONE Discord message to channel %(channel)s, and a HARD CEILING OF 400 CHARACTERS. Not a
target — a limit. Three lines at most:

    **`<branch>` synced** — <what the conflict was, one clause> · [PR #<n>](<<pr url>>)
    <one line only if a human must decide something, saying exactly what>

MORE THAN ONE ITEM IS A MARKDOWN LIST (`- item` per line). Discord lays out the FIRST line of a
message differently from those after it, so a run of plain prefixed lines comes out ragged. One item
needs no list.

Link from the NUMBER, never paste the URL: `[PR #715](<https://.../pull/715>)`. The angle brackets
inside the target are what stop Discord expanding it into a preview card, and the reader gets a
short clickable reference instead of forty characters of github.com path.

That channel has several human readers who do not run this estate and did not ask for a status
report. Your last run posted 11,000 characters across nine messages about three one-line outcomes,
and the single fact worth reading — that one branch was now redundant — was buried in the middle of
the eighth. Judge every sentence by whether a reader who only cares about the REPOSITORY would miss
it if it were gone.

NEVER post about this machine. Not the local clone, not paths under ~, not hooks, not permissions,
not what you tried first, not what refused you, not your own reasoning or workflow. The operator
does not administer these boxes and it is not his problem; exposing several people to it is worse
than useless. If something here blocked you, that goes in your RETURN STATUS, which reaches the
estate journal where it belongs — and then say on Discord only "blocked, not landed", with no
explanation of why.

Return a one-line status: what you did, and any local blocker in full. That line is for the estate,
not the channel, so put the machine detail THERE."""


def dispatch(stuck, run, state):
    """Wake M4 for conflicts that are new to it. Returns the list of branches dispatched."""
    if not DISPATCH:
        return []
    sys.path.insert(0, str(HERE.parents[1] / "agents"))
    try:
        from run import run_agent
    except ImportError as e:
        errlog.err("branch_sync_watch: cannot import the agent runner — no conflict will be "
                   "escalated to M4, they will only be reported", e)
        return []
    seen = set(state.get("dispatched", []))
    todo = [b for b in stuck if b.get("action") in DISPATCH_ACTIONS and sig(b) not in seen]
    if not todo:
        return []
    if len(todo) > MAX_DISPATCH:
        # Say what is being dropped. A silent cap reads as "these were handled".
        errlog.warn("branch_sync_watch: %d new conflicts, dispatching %d — NOT escalating: %s"
                    % (len(todo), MAX_DISPATCH,
                       ", ".join(b["branch"] for b in todo[MAX_DISPATCH:])))
        todo = todo[:MAX_DISPATCH]
    done = []
    for b in todo:
        first = b.get("conflict_first") or {}
        where = ("%s:%s — %s | branch had %s | master had %s"
                 % (first.get("file"), first.get("line"), first.get("why"),
                    "; ".join(first.get("ours") or []) or "(nothing)",
                    "; ".join(first.get("theirs") or []) or "(nothing)")
                 ) if first.get("file") else "(the workflow reported no hunk detail)"
        # A PR for a sync merge is noise: it asks a human to review master's own commits arriving
        # on a branch, which the branch-sync Action already does unattended everywhere else. So an
        # unprotected branch takes the merge directly. Only master and dev-* — the two GitHub
        # actually protects — need a PR, and there it is a human who must land it.
        sens = ("`%s` is PROTECTED. Open a PR: push `ai-feature-sync-%s`, then `gh pr create "
                "--title \"M4: sync %s with master\" --base %s --reviewer "
                + ",".join(PR_REVIEWERS) + " --assignee " + ",".join(PR_REVIEWERS)
                + "`. EVERY PR title you open starts with `M4: ` so the list shows at a glance "
                "which were raised by a machine. NEVER open one with no reviewer requested — the "
                "operator then has to request a review from himself before he can approve it. "
                "Only a human may merge it."
                % ((b["branch"],) * 4) if protected(b["branch"]) else
                "`%s` is not protected, so land the merge on it DIRECTLY — "
                "`git push origin ai-feature-sync-%s:%s`. No PR: a pull request asking someone to "
                "review master's own commits arriving on a branch is pure noise. A merge only "
                "appends, so every clone still fast-forwards."
                % ((b["branch"],) * 3))
        try:
            reply = run_agent("theming", PROMPT % {
                "branch": b["branch"], "ahead": b["ahead"], "behind": b["behind"],
                "files": ", ".join(b.get("conflict_files") or []) or "(none listed)",
                "first": where, "url": run["url"], "sensitivity": sens,
                "channel": discord_api.channel()}, timeout=900)
            print("dispatched %s -> %s" % (b["branch"], (reply or "")[:120]))
            done.append(b["branch"])
            seen.add(sig(b))
        except Exception as e:
            # Do NOT record it as dispatched: a failed wake should be retried next run, not
            # silently retired the way a successful one is.
            errlog.err("branch_sync_watch: dispatching M4 for %s failed — that conflict was "
                       "reported but nobody was asked to act on it" % b["branch"], e)
    state["dispatched"] = sorted(seen)
    return done


def main():
    try:
        status, run = latest_status()
    except Exception as e:
        errlog.err("branch_sync_watch: fetching latest status", e)
        return 1
    if not status:
        print("no completed branch-sync runs yet")
        return 0

    stuck = [b for b in status["branches"] if b.get("action") in STUCK]
    names = {b["branch"] for b in stuck}
    try:
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
    except Exception as e:
        errlog.skip("branch_sync_watch: reading state", e)
        state = {}
    seen = set(state.get("stuck", []))
    cleared = seen - names

    if names == seen:
        print("no change: %d stuck (%s)" % (len(names), ", ".join(sorted(names)) or "none"))
    elif names:
        # ONLY when something needs a human. The last branch clearing used to post "all branches
        # clear", which is a message whose entire content is that there is nothing to do — the
        # purest form of the noise this channel is not for. A clear estate is reported by silence.
        # (When the set shrinks but is not empty, render() still names what cleared, inside a post
        # that was going out anyway.)
        text = render(status, run, stuck, cleared)
        try:
            discord_api.post_embed(text, color=0xE3B341,
                                   title="Branch sync — %d branch(es) need a human" % len(names))
            print("posted to discord: %d need a human" % len(names))
        except Exception as e:
            errlog.err("branch_sync_watch: posting to discord", e)
            return 1        # do NOT record as seen; retry next run rather than swallow the alert
    elif cleared:
        print("cleared, nothing outstanding (%s) — not posting" % ", ".join(sorted(cleared)))

    # Deliberately OUTSIDE the "did the set change" test above. That test asks whether the REPORT is
    # news; this asks whether the agent has ever been given this conflict, and the two are different
    # questions. The branches in this report had been stuck, unchanged and unattended, for months —
    # under the old flow that sameness was exactly what kept anything from happening.
    # Protected branches: keep a sync PR open and current. Triggered on BEHIND, not on the action
    # label — "push-rejected" and "needs-pr" are the same fact wearing two names depending on
    # whether theming#706 has landed, and a branch that has drifted needs the PR either way.
    notes, conflicted = [], []
    for b in status["branches"]:
        if not protected(b["branch"]) or b["branch"] in ("master", "main"):
            continue
        if int(b.get("behind") or 0) <= 0:
            continue
        r = sync_pr(b["branch"])
        if r == "CONFLICT":
            # Only now is there a decision to make, so only now is an agent worth waking.
            conflicted.append(dict(b, action="conflict",
                                   conflict_files=b.get("conflict_files") or []))
        elif r:
            notes.append(r)
    notes += merge_approved_sync_prs()
    if notes:
        try:
            discord_api.post(discord_api.items(notes))
            print("posted:", "; ".join(notes))
        except Exception as e:
            errlog.err("branch_sync_watch: posting the sync-PR note failed", e)

    for br in dispatch(stuck + conflicted, run, state):
        print("M4 dispatched for", br)

    state.update({"stuck": sorted(names), "run": run["databaseId"],
                  "generated": status["generated"]})
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
