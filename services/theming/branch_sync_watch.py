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
    lines.append("_%s · base %s · %s_" % (counts, status.get("base_sha", "?"), run["url"]))
    # This posts through the same bot account M4 replies from, so it looks like M4 spoke — and it
    # is not M4, it is a cron job with no ability to reason about what it just pasted. Say so, and
    # point at the thing that CAN: @M4 reads this channel and can open the repo.
    lines.append("_automated report — @M4 for a diagnosis of any line_")
    return "\n".join(lines)


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
        seen = set(json.loads(STATE.read_text()).get("stuck", [])) if STATE.exists() else set()
    except Exception as e:
        errlog.skip("branch_sync_watch: reading state", e)
        seen = set()

    cleared = seen - names
    if names == seen:
        print("no change: %d stuck (%s)" % (len(names), ", ".join(sorted(names)) or "none"))
        return 0

    if names or cleared:
        text = render(status, run, stuck, cleared)
        title = ("Branch sync — %d branch(es) need a human" % len(names)) if names \
                else "Branch sync — all branches clear"
        try:
            discord_api.post_embed(text, title=title, color=0xE3B341 if names else 0x2EA043)
            print("posted to discord:", title)
        except Exception as e:
            errlog.err("branch_sync_watch: posting to discord", e)
            return 1        # do NOT record as seen; retry next run rather than swallow the alert

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"stuck": sorted(names), "run": run["databaseId"],
                                 "generated": status["generated"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
